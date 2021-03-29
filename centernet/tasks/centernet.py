import tensorflow as tf
from tensorflow.keras.mixed_precision import experimental as mixed_precision

from absl import logging
from official.core import base_task
from official.core import input_reader
from official.core import task_factory
from official.vision.beta.evaluation import coco_evaluator

# TODO: Already added to official codebase, change to official version later
from yolo.dataloaders.decoders import tfds_coco_decoder

from centernet.configs import centernet as cfg
from centernet.dataloaders import centernet_input
from centernet.modeling.CenterNet import build_centernet
from centernet.ops import loss_ops
from centernet.losses import penalty_reduced_logistic_focal_loss
from centernet.losses import l1_localization_loss


@task_factory.register_task_cls(cfg.CenterNetTask)
class CenterNetTask(base_task.Task):

  def __init__(self, params, logging_dir: str = None):
    super().__init__(params, logging_dir)

  def build_inputs(self, params, input_context=None):
    """Build input dataset."""
    decoder = tfds_coco_decoder.MSCOCODecoder()
    """
    decoder_cfg = params.decoder.get()
    if params.decoder.type == 'simple_decoder':
        decoder = tf_example_decoder.TfExampleDecoder(
            regenerate_source_id=decoder_cfg.regenerate_source_id)
    elif params.decoder.type == 'label_map_decoder':
        decoder = tf_example_label_map_decoder.TfExampleDecoderLabelMap(
            label_map=decoder_cfg.label_map,
            regenerate_source_id=decoder_cfg.regenerate_source_id)
    else:
        raise ValueError('Unknown decoder type: {}!'.format(params.decoder.type))
    """

    model = self.task_config.model

    masks, path_scales, xy_scales = self._get_masks()
    anchors = self._get_boxes(gen_boxes=params.is_training)

    print(masks, path_scales, xy_scales)
    parser = centernet_input.CenterNetParser(
        num_classes=model.num_classes,
        gaussian_iou=model.gaussian_iou
    )

    if params.is_training:
      post_process_fn = parser.postprocess_fn()
    else:
      post_process_fn = None

    reader = input_reader.InputReader(
        params,
        dataset_fn=tf.data.TFRecordDataset,
        decoder_fn=decoder.decode,
        parser_fn=parser.parse_fn(params.is_training),
        postprocess_fn=post_process_fn)
    dataset = reader.read(input_context=input_context)
    return dataset

  def build_model(self):
    """get an instance of CenterNet"""
    params = self.task_config.train_data
    model_base_cfg = self.task_config.model
    l2_weight_decay = self.task_config.weight_decay / 2.0

    input_specs = tf.keras.layers.InputSpec(shape=[None] +
                                            model_base_cfg.input_size)
    l2_regularizer = (
      tf.keras.regularizers.l2(l2_weight_decay) if l2_weight_decay else None)

    model, losses = build_centernet(input_specs, self.task_config, l2_regularizer)
    self._loss_dict = losses
    return model

  def build_inputs(self, params, input_context=None):
    pass

  def build_losses(self, outputs, labels, aux_losses=None):
    total_loss = 0.0
    total_scale_loss = 0.0
    total_offset_loss = 0.0
    loss = 0.0
    scale_loss = 0.0
    offset_loss = 0.0

    metric_dict = dict()

    # TODO: Calculate loss
    flattened_ct_heatmaps = loss_ops._flatten_spatial_dimensions(labels['ct_heatmaps'])
    num_boxes = loss_ops._to_float32(loss_ops.get_num_instances_from_weights(labels['tag_masks']))

    object_center_loss = penalty_reduced_logistic_focal_loss.PenaltyReducedLogisticFocalLoss(reduction=tf.keras.losses.Reduction.NONE)

    outputs['ct_heatmaps'] = loss_ops._flatten_spatial_dimensions(outputs['ct_heatmaps'])
    total_loss += object_center_loss(
        flattened_ct_heatmaps, outputs['ct_heatmaps'])  #removed weight parameter (weight = per_pixel_weight)
    center_loss = tf.reduce_sum(total_loss) / (
        float(len(outputs['ct_heatmaps'])) * num_boxes)
    loss += center_loss
    metric_dict['ct_loss'] = center_loss

    localization_loss_fn = l1_localization_loss.L1LocalizationLoss(reduction=tf.keras.losses.Reduction.NONE)
    # Compute the scale loss.
    scale_pred = outputs['ct_size']
    offset_pred = outputs['ct_offset']
    total_scale_loss += localization_loss_fn(
        labels['ct_size'], scale_pred)                #removed  weights=batch_weights
    # Compute the offset loss.
    total_offset_loss += localization_loss_fn(
        labels['ct_offset'], offset_pred)             #removed weights=batch_weights
    scale_loss += tf.reduce_sum(total_scale_loss) / (
        float(len(outputs['ct_size'])) * num_boxes)
    offset_loss += tf.reduce_sum(total_offset_loss) / (
        float(len(outputs['ct_size'])) * num_boxes)
    metric_dict['ct_scale_loss'] = scale_loss
    metric_dict['ct_offset_loss'] = offset_loss

    return loss, metric_dict

  def build_metrics(self, training=True):
    metrics = []
    metric_names = self._metric_names

    for name in metric_names:
      metrics.append(tf.keras.metrics.Mean(name, dtype=tf.float32))

    self._metrics = metrics
    if not training:
      self.coco_metric = coco_evaluator.COCOEvaluator(
          annotation_file=self.task_config.annotation_file,
          include_mask=False,
          need_rescale_bboxes=False,
          per_category_metrics=self._task_config.per_category_metrics)
    return metrics

  def train_step(self, inputs, model, optimizer, metrics=None):
    pass
  
  def validation_step(self, inputs, model, metrics=None):
    # get the data point
    image, label = inputs

    # computer detivative and apply gradients
    y_pred = model(image, training=False)
    y_pred = tf.nest.map_structure(lambda x: tf.cast(x, tf.float32), y_pred['raw_output'])
    loss_metrics = self.build_losses(y_pred, label)
    logs = {self.loss: loss_metrics['total_loss']}

    coco_model_outputs = {
      'detection_boxes':
          box_ops.denormalize_boxes(
              tf.cast(y_pred['bbox'], tf.float32), image_shape),
      'detection_scores':
          y_pred['confidence'],
      'detection_classes':
          y_pred['classes'],
      'num_detections':
          tf.shape(y_pred['bbox'])[:-1],
    }

    logs.update({self.coco_metric.name: (label, coco_model_outputs)})

    if metrics:
      for m in metrics:
        m.update_state(loss_metrics[m.name])
        logs.update({m.name: m.result()})

    return logs

  def aggregate_logs(self, state=None, step_outputs=None):
    pass

  def reduce_aggregated_logs(self, aggregated_logs):
    pass

  def _get_masks(self,
                 xy_exponential=True,
                 exp_base=2,
                 xy_scale_base='default_value'):
    pass

  def initialize(self, model: tf.keras.Model):
    pass
