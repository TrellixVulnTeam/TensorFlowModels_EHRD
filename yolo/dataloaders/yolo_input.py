""" Detection Data parser and processing for YOLO.
Parse image and ground truths in a dataset to training targets and package them
into (image, labels) tuple for RetinaNet.
"""

# Import libraries
import tensorflow as tf
import tensorflow_addons as tfa

from yolo.ops import preprocessing_ops
from yolo.ops import box_ops as box_utils
from official.vision.beta.ops import box_ops, preprocess_ops
from official.vision.beta.dataloaders import parser
from yolo.ops import loss_utils as loss_ops


class Parser(parser.Parser):
  """Parser to parse an image and its annotations into a dictionary of tensors."""

  def __init__(self,
               image_w=416,
               image_h=416,
               num_classes=80,
               fixed_size=False,
               jitter_im=0.1,
               jitter_boxes=0.005,
               use_tie_breaker=True,
               min_level=3,
               max_level=5,
               masks=None,
               max_process_size=608,
               min_process_size=320,
               max_num_instances=200,
               random_flip=True,
               pct_rand=0.5,
               aug_rand_saturation=True,
               aug_rand_brightness=True,
               aug_rand_zoom=True,
               aug_rand_hue=True,
               anchors=None,
               seed=10,
               dtype='float32'):
    """Initializes parameters for parsing annotations in the dataset.
    Args:
      image_w: a `Tensor` or `int` for width of input image.
      image_h: a `Tensor` or `int` for height of input image.
      num_classes: a `Tensor` or `int` for the number of classes.
      fixed_size: a `bool` if True all output images have the same size.
      jitter_im: a `float` that is the maximum jitter applied to the image for
        data augmentation during training.
      jitter_boxes: a `float` that is the maximum jitter applied to the bounding
        box for data augmentation during training.
      net_down_scale: an `int` that down scales the image width and height to the
        closest multiple of net_down_scale.
      max_process_size: an `int` for maximum image width and height.
      min_process_size: an `int` for minimum image width and height ,
      max_num_instances: an `int` number of maximum number of instances in an image.
      random_flip: a `bool` if True, augment training with random horizontal flip.
      pct_rand: an `int` that prevents do_scale from becoming larger than 1-pct_rand.
      masks: a `Tensor`, `List` or `numpy.ndarrray` for anchor masks.
      aug_rand_saturation: `bool`, if True, augment training with random
        saturation.
      aug_rand_brightness: `bool`, if True, augment training with random
        brightness.
      aug_rand_zoom: `bool`, if True, augment training with random
        zoom.
      aug_rand_hue: `bool`, if True, augment training with random
        hue.
      anchors: a `Tensor`, `List` or `numpy.ndarrray` for bounding box priors.
      seed: an `int` for the seed used by tf.random
    """
    self._net_down_scale = 2**max_level

    self._num_classes = num_classes
    self._image_w = (image_w // self._net_down_scale) * self._net_down_scale
    self._image_h = self._image_w if image_h is None else (
        image_h // self._net_down_scale) * self._net_down_scale

    self._max_process_size = max_process_size
    self._min_process_size = min_process_size
    self._fixed_size = fixed_size

    self._anchors = anchors
    self._masks = {
        key: tf.convert_to_tensor(value) for key, value in masks.items()
    }
    self._use_tie_breaker = use_tie_breaker

    self._jitter_im = 0.0 if jitter_im is None else jitter_im
    self._jitter_boxes = 0.0 if jitter_boxes is None else jitter_boxes
    self._pct_rand = pct_rand
    self._max_num_instances = max_num_instances
    self._random_flip = random_flip

    self._aug_rand_saturation = aug_rand_saturation
    self._aug_rand_brightness = aug_rand_brightness
    self._aug_rand_zoom = aug_rand_zoom
    self._aug_rand_hue = aug_rand_hue

    self._seed = seed

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

  def _build_grid(self, raw_true, width, batch=False, use_tie_breaker=False):
    mask = self._masks
    for key in self._masks.keys():
      if not batch:
        mask[key] = preprocessing_ops.build_grided_gt(
            raw_true, self._masks[key], width // 2**int(key), self._num_classes,
            raw_true['bbox'].dtype, use_tie_breaker)
      else:
        mask[key] = preprocessing_ops.build_batch_grided_gt(
            raw_true, self._masks[key], width // 2**int(key), self._num_classes,
            raw_true['bbox'].dtype, use_tie_breaker)

      mask[key] = tf.cast(mask[key], self._dtype)
    return mask

  def _parse_train_data(self, data):
    """Generates images and labels that are usable for model training.
        Args:
          data: a dict of Tensors produced by the decoder.
        Returns:
          images: the image tensor.
          labels: a dict of Tensors that contains labels.
        """

    shape = tf.shape(data['image'])
    image = data['image']/255 
    
    #/ 255
    boxes = data['groundtruth_boxes']
    classes = data['groundtruth_classes']
    width = shape[0]
    height = shape[1]

    do_blur = tf.random.uniform([], minval= 0,maxval=1, seed=self._seed, dtype=tf.float32)
    if do_blur > 0.4:
      image = tfa.image.gaussian_filter2d(image, filter_shape = 5, sigma = 3)
    if do_blur > 0.6:
      image = tfa.image.gaussian_filter2d(image, filter_shape = 5, sigma = 6)
    elif do_blur > 0.7:
      image = tfa.image.gaussian_filter2d(image, filter_shape = 5, sigma = 10)
    elif do_blur > 0.9:
      image = tfa.image.gaussian_filter2d(image, filter_shape = 5, sigma = 12)

    if self._aug_rand_brightness:
      delta = tf.random.uniform([], minval= -0.5,maxval=0.5, seed=self._seed, dtype=tf.float32)
      image = tf.image.adjust_brightness(image, delta)
    if self._aug_rand_saturation:
      delta = tf.random.uniform([], minval= 0.1,maxval=1.5, seed=self._seed, dtype=tf.float32)
      image = tf.image.adjust_saturation(image, delta)
    if self._aug_rand_hue:
      delta = tf.random.uniform([], minval= -0.1,maxval=0.1, seed=self._seed, dtype=tf.float32)
      image = tf.image.adjust_hue(image, delta)  # Hue
    
    stddev = tf.random.uniform([], minval= 0.0, maxval=40/255, seed=self._seed, dtype=tf.float32)
    noise = tf.random.normal(shape = tf.shape(image), mean = 0.0, stddev = stddev, seed=self._seed)
    image += noise 
    image = tf.clip_by_value(image, 0.0, 1.0)

    
    image, boxes = preprocessing_ops.fit_preserve_aspect_ratio(
        image,
        boxes,
        width=width,
        height=height,
        target_dim=self._max_process_size)

    image_shape = tf.shape(image)[:2]

    if self._random_flip:
      image, boxes, _ = preprocess_ops.random_horizontal_flip(
          image, boxes, seed=self._seed)

    randscale = self._image_w // self._net_down_scale

    if self._fixed_size:
      do_scale = tf.greater(
          tf.random.uniform([], minval=0, maxval=1, seed=self._seed),
          1 - self._pct_rand)
      if do_scale:
        randscale = tf.random.uniform([],
                                      minval=10,
                                      maxval=21,
                                      seed=self._seed,
                                      dtype=tf.int32)

    if self._jitter_boxes != 0.0:
      boxes = box_ops.denormalize_boxes(boxes, image_shape)
      boxes = box_ops.jitter_boxes(boxes, 0.025)
      boxes = box_ops.normalize_boxes(boxes, image_shape)

    if self._aug_rand_zoom:

      randscale = 15
      image, boxes, classes = preprocessing_ops.resize_crop_filter(
          image,
          boxes,
          classes, 
          default_width=self._image_w,
          default_height=self._image_h,
          target_width=randscale * self._net_down_scale,
          target_height=randscale * self._net_down_scale, 
          randomize = True)

    if self._jitter_im != 0.0:
      image, boxes, classes = preprocessing_ops.random_translate(
          image, boxes, classes, self._jitter_im, seed=self._seed)
    
    boxes = box_utils.yxyx_to_xcycwh(boxes)
    image = tf.image.resize(image, (self._image_w, self._image_h), preserve_aspect_ratio=False)
    image = tf.clip_by_value(image, 0.0, 1.0)
    num_dets = tf.shape(classes)[0]

    best_anchors = preprocessing_ops.get_best_anchor(
        boxes, self._anchors, width=self._image_w, height=self._image_h)

    # padding
    boxes = preprocess_ops.clip_or_pad_to_fixed_size(boxes,self._max_num_instances, 0)
    classes = preprocess_ops.clip_or_pad_to_fixed_size(classes, self._max_num_instances, -1)
    best_anchors = preprocess_ops.clip_or_pad_to_fixed_size(best_anchors, self._max_num_instances, 0)
    # area = preprocess_ops.clip_or_pad_to_fixed_size(data['groundtruth_area'],
    #                                                 self._max_num_instances, 0)
    # is_crowd = preprocess_ops.clip_or_pad_to_fixed_size(
    #     tf.cast(data['groundtruth_is_crowd'], tf.int32),
    #     self._max_num_instances, 0)

    labels = {
        'source_id': data['source_id'],
        'bbox': tf.cast(boxes, self._dtype),
        'classes': tf.cast(classes, self._dtype),
        'best_anchors': tf.cast(best_anchors, self._dtype),
        'width': width,
        'height': height,
        'num_detections': num_dets,
    }

    if self._fixed_size:
      grid = self._build_grid(
          labels, self._image_w, use_tie_breaker=self._use_tie_breaker)
      labels.update({'grid_form': grid})
      labels['bbox'] = box_utils.xcycwh_to_yxyx(labels['bbox'])



    return image, labels

  # broken for some reason in task, i think dictionary to coco evaluator has
  # issues
  def _parse_eval_data(self, data):
    """Generates images and labels that are usable for model training.
        Args:
          data: a dict of Tensors produced by the decoder.
        Returns:
          images: the image tensor.
          labels: a dict of Tensors that contains labels.
        """

    shape = tf.shape(data['image'])
    image = data['image'] / 255
    boxes = data['groundtruth_boxes']
    width = shape[0]
    height = shape[1]

    image, boxes = preprocessing_ops.fit_preserve_aspect_ratio(
        image, boxes, width=width, height=height, target_dim=self._image_w)
    boxes = box_utils.yxyx_to_xcycwh(boxes)

    best_anchors = preprocessing_ops.get_best_anchor(
        boxes, self._anchors, width=self._image_w, height=self._image_h)
    boxes = preprocessing_ops.pad_max_instances(boxes, self._max_num_instances,
                                                0)
    classes = preprocessing_ops.pad_max_instances(data['groundtruth_classes'],
                                                  self._max_num_instances, -1)
    best_anchors = preprocessing_ops.pad_max_instances(best_anchors,
                                                       self._max_num_instances,
                                                       0)
    area = preprocessing_ops.pad_max_instances(data['groundtruth_area'],
                                               self._max_num_instances, 0)
    is_crowd = preprocessing_ops.pad_max_instances(
        tf.cast(data['groundtruth_is_crowd'], tf.int32),
        self._max_num_instances, 0)

    
    labels = {
        'source_id': data['source_id'],
        'bbox': tf.cast(boxes, self._dtype),
        'classes': tf.cast(classes, self._dtype),
        'area': tf.cast(area, self._dtype),
        'is_crowd': is_crowd,
        'best_anchors': tf.cast(best_anchors, self._dtype),
        'width': width,
        'height': height,
        'num_detections': tf.shape(data['groundtruth_classes'])[0],
    }

    # if self._fixed_size:
    grid = self._build_grid(
        labels,
        self._image_w,
        batch=False,
        use_tie_breaker=self._use_tie_breaker)
    labels.update({'grid_form': grid})
    labels['bbox'] = box_utils.xcycwh_to_yxyx(labels['bbox'])
    return image, labels

  def mosaic_fn(self, image, label):
    image, boxes, classes, num_instances = preprocessing_ops.randomized_cutmix_split(image, label['bbox'], label['classes'])
    best_anchors = preprocessing_ops.get_best_anchor(boxes, self._anchors, width=self._image_w, height=self._image_h)
    boxes = preprocessing_ops.pad_max_instances(boxes, self._max_num_instances,0)
    classes = preprocess_ops.clip_or_pad_to_fixed_size(classes, self._max_num_instances, -1)
    best_anchors = preprocess_ops.clip_or_pad_to_fixed_size(best_anchors, self._max_num_instances, 0)

    boxes = box_utils.yxyx_to_xcycwh(boxes)
    label['bbox'] = boxes
    label['classes'] = classes
    label['best_anchors'] = best_anchors

    del label["grid_form"]
    
    if self._fixed_size:
      grid = self._build_grid(labels, self._image_w, batch=True, use_tie_breaker=self._use_tie_breaker)
      labels.update({'grid_form': grid})
      labels['bbox'] = box_utils.xcycwh_to_yxyx(labels['bbox'])
    return image, label

  def _postprocess_fn(self, image, label):
    randscale = self._image_w // self._net_down_scale
    if not self._fixed_size:
      do_scale = tf.greater(
          tf.random.uniform([], minval=0, maxval=1, seed=self._seed),
          1 - self._pct_rand)
      if do_scale:
        randscale = tf.random.uniform([],
                                      minval=10,
                                      maxval=20,
                                      seed=self._seed,
                                      dtype=tf.int32)
    width = randscale * self._net_down_scale
    image = tf.image.resize(image, (width, width))
    grid = self._build_grid(
        label, width, batch=True, use_tie_breaker=self._use_tie_breaker)
    label.update({'grid_form': grid})
    label['bbox'] = box_utils.xcycwh_to_yxyx(label['bbox'])
    return image, label

  def postprocess_fn(self):
    return self._postprocess_fn if not self._fixed_size else None
