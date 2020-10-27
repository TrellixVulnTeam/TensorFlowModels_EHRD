import numpy as np
import pickle
import tensorflow_datasets as tfds
import matplotlib.pyplot as plt
from yolo.utils.iou_utils import compute_iou
from yolo.utils.box_utils import _yxyx_to_xcycwh
import tensorflow as tf


class YoloKmeans:
    """K-means for YOLO anchor box priors
    Args:
        boxes(np.ndarray): a matrix containing image widths and heights
        k(int): number of clusters
        with_color(bool): color map
    To use:
        km = YoloKmeans(boxes = np.random.rand(20, 2), k = 3, with_color = True)
        centroids, map = km.run_kmeans()
        
        km = YoloKmeans()
        km.load_voc_boxes()
        centroids = km.run_kmeans()

        km = YoloKmeans()
        km.get_box_from_file("voc_boxes.pkl")
        centroids = km.run_kmeans()

        km = YoloKmeans()
        km.get_box_from_dataset(tfds.load('voc', split=['train', 'test', 'validation']))
        centroids = km.run_kmeans()
    """
    def __init__(self, boxes=None, k=9, with_color=False):

        assert isinstance(k, int)
        assert isinstance(with_color, bool)

        self._k = k
        self._boxes = boxes
        self._with_color = with_color

    def iou(self, boxes, clusters):
        n = boxes.shape[0]
        boxes = tf.repeat(boxes, self._k, axis=0)
        boxes = tf.reshape(boxes, (n, self._k, -1))
        boxes = tf.cast(boxes, tf.float32)

        clusters = tf.tile(clusters, [n, 1])
        clusters = tf.reshape(clusters, (n, self._k, -1))
        clusters = tf.cast(clusters, tf.float32)

        zeros = tf.cast(tf.zeros(boxes.shape), dtype=tf.float32)

        boxes = tf.concat([zeros, boxes], axis=-1)
        clusters = tf.concat([zeros, clusters], axis=-1)
        return compute_iou(boxes, clusters)

    def get_box_from_file(self, filename):
        try:
            f = open(filename, 'rb')
        except IOError:
            pass
        self._boxes = pickle.load(f)

    def get_box_from_dataset(self, dataset):
        box_ls = []
        if not isinstance(dataset, list):
            dataset = [dataset]
        for ds in dataset:
            for el in ds:
                for box in list(el['objects']['bbox']):
                    box_ls.append(_yxyx_to_xcycwh(box).numpy()[..., 2:])
        self._boxes = np.array(box_ls)

    def load_voc_boxes(self):
        self.get_box_from_dataset(
            tfds.load('voc', split=['train', 'test', 'validation']))

    def load_coco_boxes(self):
        self.get_box_from_dataset(
            tfds.load('coco',
                      split=['test', 'test2015', 'train', 'validation']))

    def get_boxes(self):
        return self._boxes

    def run_kmeans(self, max_iter=300):
        if not isinstance(self._boxes, np.ndarray):
            raise Exception('Box Not found')

        box_num = self._boxes.shape[0]
        k = self._k
        dists = np.zeros((box_num, k))
        last = np.zeros((box_num, ))
        np.random.seed()
        clusters = self._boxes[np.random.choice(box_num, k, replace=False)]
        num_iters = 0

        while num_iters < max_iter:
            dists = 1 - self.iou(self._boxes, clusters)
            curr = np.argmin(dists, axis=-1)
            if (curr == last).all():
                break
            for i in range(k):
                clusters[i] = np.mean(self._boxes[curr == i], axis=0)
            last = curr
            num_iters += 1
        print(f'num_iters = {num_iters}')
        clusters = np.array(sorted(clusters, key=lambda x: x[0] * x[1]))
        if self._with_color:
            return clusters, last
        else:
            return clusters, None


class MiniBatchKMeansNN():
    def __init__(self, boxes=None, k=9):
        assert isinstance(k, int)
        self._k = k
    
    @tf.function(experimental_relax_shapes=True)
    def compute_iou(self, sample, clusters):
        boxes = sample[..., 2:]
        n = tf.shape(boxes)[0]
        boxes = tf.repeat(tf.expand_dims(boxes, axis = 0), repeats = self._k, axis = 0)
        clusters = tf.repeat(clusters, repeats = n, axis = 1)
        zero_xy = tf.zeros_like(clusters)
        boxes = tf.concat([zero_xy, boxes], axis = -1)
        clusters = tf.concat([zero_xy, clusters], axis = -1)
        return compute_iou(boxes, clusters)

    @tf.function(experimental_relax_shapes=True)
    def get_boxes_in_cluster(self, boxes, iou, cluster_sum, boxes_in_cluster):
        box_list = []
        lens = []
        indexes = tf.math.argmax(iou, axis=0)
        for i in range(self._k):  
            boxes_ = tf.boolean_mask(boxes, indexes == i, axis=0)
            value = tf.math.reduce_sum(boxes_, axis = 0)
            lens.append(tf.shape(boxes_)[0])
            box_list.append(value[..., 2:])
        cluster_sum += tf.stack(box_list, axis = 0)
        boxes_in_cluster += tf.cast(tf.stack(lens, axis = 0), dtype = tf.float32)
        return cluster_sum, boxes_in_cluster

    @tf.function(experimental_relax_shapes=True)
    def train_step(self, boxes, clusters, cluster_sum, boxes_in_cluster):
        boxes = _yxyx_to_xcycwh(boxes)
        iou = self.compute_iou(boxes, clusters)
        cluster_sum, boxes_in_cluster = self.get_boxes_in_cluster(boxes, iou, cluster_sum, boxes_in_cluster)
        return cluster_sum, boxes_in_cluster
    
    @tf.function(experimental_relax_shapes=True)
    def update_step(self, cluster_sum, boxes_in_cluster):
        clusters = tf.expand_dims(cluster_sum/tf.stack((boxes_in_cluster, boxes_in_cluster), axis = -1), axis = 1)
        cluster_sum = tf.zeros(shape= (self._k, 2), dtype = tf.float32)
        boxes_in_cluster = tf.zeros(shape= (self._k, ), dtype = tf.float32)
        return clusters, cluster_sum, boxes_in_cluster

    def __call__(self, dataset, mean_steps = 2000, epochs = 10):
        mean_clusters = tf.random.uniform(minval = 0, maxval = 1, shape= (self._k, 1, 2), dtype = tf.float32)
        cluster_sum = tf.zeros(shape= (self._k, 2), dtype = tf.float32)
        boxes_in_cluster = tf.zeros(shape= (self._k, ), dtype = tf.float32)
        minibatch_counter = 1
        counter = 1

        try:
            for j in range(epochs):
                clusters = mean_clusters
                for i, data in enumerate(dataset):
                    # use a thread pool of n smaples
                    minibatch_counter = (minibatch_counter + 1)%(mean_steps + 1)
                    cluster_sum, boxes_in_cluster = self.train_step(data["objects"]["bbox"], clusters, cluster_sum, boxes_in_cluster)
                    if minibatch_counter == 0:
                        clusters, cluster_sum, boxes_in_cluster = self.update_step(cluster_sum, boxes_in_cluster)
                        #tf.print(tf.squeeze(clusters, axis=1) * 416, summarize = -1, end = "\n\n")
                        mean_clusters += clusters
                        counter += 1
                    tf.print(minibatch_counter, i, j, end="\r")
                mean_clusters = mean_clusters/counter
                counter = 1
            clusters = tf.squeeze(clusters, axis=1)
            clusters = clusters.numpy()
            clusters = np.array(sorted(clusters, key=lambda x: x[0] * x[1]))
        except KeyboardInterrupt:
            clusters = tf.squeeze(clusters, axis=1)
            clusters = clusters.numpy()
            clusters = np.array(sorted(clusters, key=lambda x: x[0] * x[1]))
        return clusters
    


if __name__ == '__main__':
    import tensorflow_datasets as tfds

    coco = tfds.load("coco", split = "train", shuffle_files = True)
    coco = coco.shuffle(10000).prefetch(10000)


    km2 = YoloKmeans(k = 9)
    km2.get_box_from_dataset(coco)
    boxes, _  = km2.run_kmeans()
    print(boxes * 416)

    km = MiniBatchKMeansNN(k = 9)
    print(km(coco, epochs=2) * 416)



