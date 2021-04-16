import tensorflow as tf

from centernet.ops import preprocessing_ops
from official.vision.beta.dataloaders import parser, utils
from yolo.ops import preprocessing_ops as yolo_preprocessing_ops


def pad_max_instances(value, instances, pad_value=0, pad_axis=0):
  shape = tf.shape(value)
  if pad_axis < 0:
    pad_axis = tf.shape(shape)[0] + pad_axis
  dim1 = shape[pad_axis]
  take = tf.math.reduce_min([instances, dim1])
  value, _ = tf.split(
      value, [take, -1], axis=pad_axis)  # value[:instances, ...]
  pad = tf.convert_to_tensor([tf.math.reduce_max([instances - dim1, 0])])
  nshape = tf.concat([shape[:pad_axis], pad, shape[(pad_axis + 1):]], axis=0)
  pad_tensor = tf.fill(nshape, tf.cast(pad_value, dtype=value.dtype))
  value = tf.concat([value, pad_tensor], axis=pad_axis)
  return value

class CenterNetParser(parser.Parser):
  """ Parser to parse an image and its annotations into a dictionary of tensors """

  def __init__(self,
               image_w: int = 512,
               image_h: int = 512,
               num_classes: int = 90,
               max_num_instances: int = 128,
               use_gaussian_bump: bool = True,
               gaussian_rad: int = -1,
               gaussian_iou: float = 0.7,
               output_dims: int = 128,
               dtype: str = 'float32'):
    """Initializes parameters for parsing annotations in the dataset.
    Args:
      image_w: A `Tensor` or `int` for width of input image.
      image_h: A `Tensor` or `int` for height of input image.
      num_classes: A `Tensor` or `int` for the number of classes.
      max_num_instances: An `int` number of maximum number of instances in an image.
      use_gaussian_bump: A `boolean` indicating whether or not to splat a
        gaussian onto the heatmaps. If set to False, a value of 1 is placed at 
        the would-be center of the gaussian.
      gaussian_rad: A `int` for the desired radius of the gaussian. If this
        value is set to -1, then the radius is computed using gaussian_iou. 
      gaussian_iou: A `float` number for the minimum desired IOU used when
        determining the gaussian radius of center locations in the heatmap.
      output_dims: A `Tensor` or `int` for output dimensions of the heatmap.
    """
    self._image_w = image_w
    self._image_h = image_h
    self._num_classes = num_classes
    self._max_num_instances = max_num_instances
    self._gaussian_iou = gaussian_iou
    self._use_gaussian_bump = True
    self._gaussian_rad = -1
    self._output_dims = output_dims

    if dtype == 'float16':
      self._dtype = tf.float16
    elif dtype == 'bfloat16':
      self._dtype = tf.bfloat16
    elif dtype == 'float32':
      self._dtype = tf.float32
    else:
      raise Exception(
          'Unsupported datatype used in parser only {float16, bfloat16, or float32}'
      )
  
  def _build_heatmap_and_regressed_features(self, 
                                            labels,
                                            output_size=[128, 128], 
                                            input_size=[512, 512]):
    """ Generates the ground truth labels for centernet.
    
    Ground truth labels are generated by splatting gaussians on heatmaps for
    corners and centers. Regressed features (offsets and sizes) are also
    generated.

    Args:
      labels: A dictionary of COCO ground truth labels with at minimum the following fields:
        bbox: A `Tensor` of shape [max_num_instances, num_boxes, 4], where the last dimension
          corresponds to the top left x, top left y, bottom right x, and
          bottom left y coordinates of the bounding box
        classes: A `Tensor` of shape [max_num_instances, num_boxes] that contains the class of each
          box, given in the same order as the boxes
        num_detections: A `Tensor` or int that gives the number of objects in the image
      output_size: A `list` of length 2 containing the desired output height 
        and width of the heatmaps
      input_size: A `list` of length 2 the expected input height and width of 
        the image
    Returns:
      Dictionary of labels with the following fields:
        'tl_heatmaps': A `Tensor` of shape [output_h, output_w, num_classes],
          heatmap with splatted gaussians centered at the positions and channels
          corresponding to the top left location and class of the object
        'br_heatmaps': `Tensor` of shape [output_h, output_w, num_classes],
          heatmap with splatted gaussians centered at the positions and channels
          corresponding to the bottom right location and class of the object
        'ct_heatmaps': Tensor of shape [output_h, output_w, num_classes],
          heatmap with splatted gaussians centered at the positions and channels
          corresponding to the center location and class of the object
        'tl_offset': `Tensor` of shape [max_num_instances, 2], where the first
          num_boxes entries contain the x-offset and y-offset of the top-left
          corner of an object. All other entires are 0
        'br_offset': `Tensor` of shape [max_num_instances, 2], where the first
          num_boxes entries contain the x-offset and y-offset of the 
          bottom-right corner of an object. All other entires are 0
        'ct_offset': `Tensor` of shape [max_num_instances, 2], where the first
          num_boxes entries contain the x-offset and y-offset of the center of 
          an object. All other entires are 0
        'size': `Tensor` of shape [max_num_instances, 2], where the first
          num_boxes entries contain the width and height of an object. All 
          other entires are 0
        'box_mask': `Tensor` of shape [max_num_instances], where the first
          num_boxes entries are 1. All other entires are 0
        'box_indices': `Tensor` of shape [max_num_instances, 2], where the first
          num_boxes entries contain the y-center and x-center of a valid box. 
          These are used to extract the regressed box features from the 
          prediction when computing the loss
    """

    # boxes and classes are cast to self._dtype already from build_label
    boxes = labels['bbox']
    classes = labels['classes']
    input_size = tf.cast(input_size, self._dtype)
    output_size = tf.cast(output_size, self._dtype)
    input_h, input_w = input_size[0], input_size[1]
    output_h, output_w = output_size[0], output_size[1]
    
    # We will transpose the heatmaps at the end
    tl_heatmaps = tf.zeros((self._num_classes, output_h, output_w), dtype=self._dtype)
    br_heatmaps = tf.zeros((self._num_classes, output_h, output_w), dtype=self._dtype)
    ct_heatmaps = tf.zeros((self._num_classes, output_h, output_w), dtype=self._dtype)
    
    # Maps for offset and size predictions
    tl_offset = tf.zeros((self._max_num_instances, 2), dtype=self._dtype)
    br_offset = tf.zeros((self._max_num_instances, 2), dtype=self._dtype)
    ct_offset = tf.zeros((self._max_num_instances, 2), dtype=self._dtype)
    size      = tf.zeros((self._max_num_instances, 2), dtype=self._dtype)
    
    # Masks for valid boxes
    box_mask = tf.zeros((self._max_num_instances), dtype=tf.int32)
    box_indices  = tf.zeros((self._max_num_instances, 2), dtype=tf.int32)

    # Scaling factor for determining center/corners
    width_ratio = output_w / input_w
    height_ratio = output_h / input_h

    num_objects = labels['num_detections']

    height = tf.cast(0.0, self._dtype)
    width = tf.cast(0.0, self._dtype)
    for tag_ind in tf.range(num_objects):
      box = boxes[tag_ind]
      obj_class = classes[tag_ind] - 1 # TODO: See if subtracting 1 from the class like the paper is unnecessary

      ytl, xtl, ybr, xbr = box[0], box[1], box[2], box[3]

      xct, yct = (
        (xtl + xbr) / 2,
        (ytl + ybr) / 2
      )

      # Scale center and corner locations
      # These should be dtype=float32
      fxtl = (xtl * width_ratio)
      fytl = (ytl * height_ratio)
      fxbr = (xbr * width_ratio)
      fybr = (ybr * height_ratio)
      fxct = (xct * width_ratio)
      fyct = (yct * height_ratio)

      # Fit center and corners onto the output image
      # These should be dtype=float32
      xtl = tf.math.floor(fxtl)
      ytl = tf.math.floor(fytl)
      xbr = tf.math.floor(fxbr)
      ybr = tf.math.floor(fybr)
      xct = tf.math.floor(fxct)
      yct = tf.math.floor(fyct)
      
      # Splat gaussian at for the center/corner heatmaps
      if self._use_gaussian_bump:
        # Check: do we need to normalize these boxes?
        width = box[3] - box[1]
        height = box[2] - box[0]

        width = tf.math.ceil(width * width_ratio)
        height = tf.math.ceil(height * height_ratio)

        if self._gaussian_rad == -1:
          radius = preprocessing_ops.gaussian_radius((height, width), self._gaussian_iou)
          radius = tf.math.maximum(tf.cast(0.0, radius.dtype), tf.math.floor(radius))
        else:
          radius = self._gaussian_rad
        
        tl_heatmaps = preprocessing_ops.draw_gaussian(tl_heatmaps, [[obj_class, xtl, ytl, radius]])
        br_heatmaps = preprocessing_ops.draw_gaussian(br_heatmaps, [[obj_class, xbr, ybr, radius]])
        ct_heatmaps = preprocessing_ops.draw_gaussian(ct_heatmaps, [[obj_class, xct, yct, radius]], scaling_factor=5)

      else:
        tl_heatmaps = tf.tensor_scatter_nd_update(tl_heatmaps, 
          [[tf.cast(obj_class, tf.int32), tf.cast(ytl, tf.int32), tf.cast(xtl, tf.int32)]], [1])
        br_heatmaps = tf.tensor_scatter_nd_update(br_heatmaps, 
          [[tf.cast(obj_class, tf.int32), tf.cast(ybr, tf.int32), tf.cast(xbr, tf.int32)]], [1])
        ct_heatmaps = tf.tensor_scatter_nd_update(ct_heatmaps, 
          [[tf.cast(obj_class, tf.int32), tf.cast(yct, tf.int32), tf.cast(xct, tf.int32)]], [1])
      
      # Add box offset and size to the ground truth
      tl_offset = tf.tensor_scatter_nd_update(tl_offset, [[tag_ind, 0], [tag_ind, 1]], [fxtl - xtl, fytl - ytl])
      br_offset = tf.tensor_scatter_nd_update(br_offset, [[tag_ind, 0], [tag_ind, 1]], [fxbr - xbr, fybr - ybr])
      ct_offset = tf.tensor_scatter_nd_update(ct_offset, [[tag_ind, 0], [tag_ind, 1]], [fxct - xct, fyct - yct])
      size      = tf.tensor_scatter_nd_update(size, [[tag_ind, 0], [tag_ind, 1]], [width, height])

      # Initialy the mask is zeros, but each valid box needs to be unmasked
      box_mask = tf.tensor_scatter_nd_update(box_mask, [[tag_ind]], [1])

      # Contains the y and x coordinate of the box center in the heatmap
      box_indices = tf.tensor_scatter_nd_update(box_indices, [[tag_ind, 0], [tag_ind, 1]], [yct, xct])

    # Make heatmaps of shape [height, width, num_classes]
    tl_heatmaps = tf.transpose(tl_heatmaps, perm=[1, 2, 0])
    br_heatmaps = tf.transpose(br_heatmaps, perm=[1, 2, 0])
    ct_heatmaps = tf.transpose(ct_heatmaps, perm=[1, 2, 0])

    labels = {
      'tl_heatmaps': tl_heatmaps,
      'br_heatmaps': br_heatmaps,
      'ct_heatmaps': ct_heatmaps,
      'tl_offset': tl_offset,
      'br_offset': br_offset,
      'ct_offset': ct_offset,
      'size': size,
      'box_mask': box_mask,
      'box_indices': box_indices
    }
    return labels

  def _parse_train_data(self, data):
    """Generates images and labels that are usable for model training.

    Args:
        data: a dict of Tensors produced by the decoder.

    Returns:
        images: the image tensor.
        labels: a dict of Tensors that contains labels.
    """
    # FIXME: This is a copy of parse eval data
    image = data['image'] / 255
    boxes = data['groundtruth_boxes']
    classes = data['groundtruth_classes']

    image, boxes, info = yolo_preprocessing_ops.letter_box(
      image, boxes, xs = 0.5, ys = 0.5, target_dim=self._image_w)

    image = tf.cast(image, self._dtype)
    shape = tf.shape(image)
    height = shape[0]
    width = shape[1]

    image, labels = self._build_label(
      image, boxes, classes, width, height, info, data, is_training=False
    )
    
    return image, labels

  def _parse_eval_data(self, data):
    """Generates images and labels that are usable for model evaluation.

    Args:
      decoded_tensors: a dict of Tensors produced by the decoder.

    Returns:
      images: the image tensor.
      labels: a dict of Tensors that contains labels.
    """
    image = data['image'] / 255
    boxes = data['groundtruth_boxes']
    classes = data['groundtruth_classes']

    image, boxes, info = yolo_preprocessing_ops.letter_box(
      image, boxes, xs = 0.5, ys = 0.5, target_dim=self._image_w)

    image = tf.cast(image, self._dtype)
    shape = tf.shape(image)
    height = shape[0]
    width = shape[1]

    image, labels = self._build_label(
      image, boxes, classes, width, height, info, data, is_training=False
    )
    
    return image, labels

  def _build_label(self, image, boxes, classes, width, height, info, data, 
                   is_training):
    imshape = image.get_shape().as_list()
    imshape[-1] = 3
    image.set_shape(imshape)

    bshape = boxes.get_shape().as_list()
    boxes = pad_max_instances(boxes, self._max_num_instances, 0)
    bshape[0] = self._max_num_instances
    boxes.set_shape(bshape)

    cshape = classes.get_shape().as_list()
    classes = pad_max_instances(classes,
                                self._max_num_instances, -1)
    cshape[0] = self._max_num_instances
    classes.set_shape(cshape)

    area = data['groundtruth_area']
    ashape = area.get_shape().as_list()
    area = pad_max_instances(area, self._max_num_instances,0)
    ashape[0] = self._max_num_instances
    area.set_shape(ashape)

    is_crowd = data['groundtruth_is_crowd']
    ishape = is_crowd.get_shape().as_list()
    is_crowd = pad_max_instances(
        tf.cast(is_crowd, tf.int32), self._max_num_instances, 0)
    ishape[0] = self._max_num_instances
    is_crowd.set_shape(ishape)

    num_detections = tf.shape(data['groundtruth_classes'])[0]
    labels = {
      'source_id': utils.process_source_id(data['source_id']),
      'bbox': tf.cast(boxes, self._dtype),
      'classes': tf.cast(classes, self._dtype),
      'area': tf.cast(area, self._dtype),
      'is_crowd': is_crowd,
      'width': width,
      'height': height,
      'info': info, 
      'num_detections': num_detections
    }

    heatmap_feature_labels = self._build_heatmap_and_regressed_features(
      labels, output_size=[self._output_dims, self._output_dims], 
      input_size=[self._image_h, self._image_w]
    )
    labels.update(heatmap_feature_labels)
    return image, labels

  def postprocess_fn(self, is_training):
    if is_training:  #or self._cutmix
      return None # if not self._fixed_size or self._mosaic else None
    else:
      return None


if __name__ == '__main__':
  # This code is for visualization
  import matplotlib.pyplot as plt
  boxes = [
    (10, 300, 15, 370),
    (100, 300, 150, 370),
    (200, 100, 15, 170),
  ]

  classes = (0, 1, 2)

  labels = CenterNetParser()._build_labels(
    tf.constant(boxes, dtype=tf.float32), 
    tf.constant(classes, dtype=tf.float32), 
    [512, 512], [512, 512]
  )
  tl_heatmaps = labels['tl_heatmaps']
  br_heatmaps = labels['br_heatmaps']
  ct_heatmaps = labels['ct_heatmaps']

  # plt.imshow(ct_heatmaps[0, ...])
  plt.imshow(ct_heatmaps[..., 1])
  # plt.imshow(ct_heatmaps[2, ...])
  plt.show()
