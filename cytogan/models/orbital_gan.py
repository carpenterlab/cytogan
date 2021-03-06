# 1. For each compound in batch:
#   1. Advance moving average
#   2. Penalize cosine distance between points of one compound and average
#   3. Find two nearest neighbors, slightly penalize closeness
# 2. For each concentration in batch:
#   1. Advance moving average
#   2. Penalize error in norm of one concentration and average
#   3. Find two nearest neighbors, slightly penalize closeness

import collections

import keras.backend as K
import numpy as np
import tensorflow as tf
from keras.layers import Input

from cytogan.models import lsgan, util

tf.logging.set_verbosity(tf.logging.INFO)

Hyper = collections.namedtuple('Hyper', [
    'image_shape',
    'generator_filters',
    'discriminator_filters',
    'generator_strides',
    'discriminator_strides',
    'latent_size',
    'noise_size',
    'initial_shape',
    'number_of_angles',
    'number_of_radii',
    'origin_label',
])


class OrbitalGAN(lsgan.LSGAN):
    def __init__(self, hyper, learning, session):
        super(OrbitalGAN, self).__init__(hyper, learning, session)

    def _train_discriminator(self, fake_images, real_images, labels,
                             with_summary):
        discriminator_labels = np.concatenate(
            [np.zeros(len(fake_images)),
             np.ones(len(real_images))], axis=0)
        images = np.concatenate([fake_images, real_images], axis=0)
        fetches = [self.optimizer['D'], self.loss['D']]
        if with_summary and self.summaries['D'] is not None:
            fetches.append(self.summaries['D'])

        angle_labels = labels

        feed_dict = {
            self.batch_size: [len(fake_images)],
            self.images: images,
            self.discriminator_labels: discriminator_labels,
            self.angle_labels: angle_labels,
            K.learning_phase(): 1,
        }
        return self.session.run(fetches, feed_dict)[1:]

    def _define_graph(self):
        super(OrbitalGAN, self)._define_graph()

        # To avoid ambguity -- these are the output (e.g. probability) labels
        self.discriminator_labels = self.labels

        batch_size = tf.cast(tf.squeeze(self.batch_size), tf.int32)

        real_latent = self.latent[batch_size:]
        self.angle_labels = Input(batch_shape=[None], dtype=tf.int32)

        real_latent = util.check_numerics(real_latent)

        self.angle_means, self.angle_variances = [], []
        angle_partitions = tf.dynamic_partition(
            real_latent,
            self.angle_labels,
            num_partitions=self.number_of_angles)
        for group in angle_partitions:
            mean, variance = tf.nn.moments(group, axes=[0])
            self.angle_means.append(mean)
            self.angle_variances.append(variance)

        self.ema = tf.train.ExponentialMovingAverage(decay=0.9999)
        update_ema_op = self.ema.apply(self.angle_means + self.angle_variances)
        moving_angle_means = []
        for mean in self.angle_means:
            mm = tf.expand_dims(self.ema.average(mean), axis=0)
            moving_angle_means.append(mm)
        moving_angle_mean_norms = K.l2_normalize(
            tf.concat(moving_angle_means, axis=0), axis=1)

        angle_losses = []
        for mean, vectors in zip(moving_angle_means, angle_partitions):
            mean_norm = K.l2_normalize(mean, axis=1)
            vector_norm = K.l2_normalize(vectors, axis=1)
            cosine_similarity = K.sum(mean_norm * vector_norm, axis=1)
            cosine_distance = K.mean(1 - cosine_similarity)

            mean_cosine_distances = 1 - K.sum(
                mean_norm * moving_angle_mean_norms, axis=1)
            closest_means = util.top_k(mean_cosine_distances, k=3)
            nearness = K.sum(closest_means[1:3])

            angle_losses.append(cosine_distance)

        self.angle_loss = tf.reduce_mean(angle_losses)
        with tf.control_dependencies([update_ema_op]):
            self.loss['D'] += 0.01 * self.angle_loss

        origin_mask = tf.equal(self.angle_labels, self.origin_label)
        origin_vectors = tf.boolean_mask(real_latent, origin_mask)
        self.origin_norm = tf.norm(tf.reduce_mean(origin_vectors, axis=0))

    def train_on_batch(self, batch, with_summary=False):
        real_images, labels = batch
        real_images = (real_images * 2.0) - 1
        batch_size = len(real_images)
        fake_images = self.generate(batch_size, rescale=False)

        d_tensors = self._train_discriminator(fake_images, real_images, labels,
                                              with_summary)
        g_tensors = self._train_generator(batch_size, None, with_summary)

        losses = dict(D=d_tensors[0], G=g_tensors[0])
        tensors = dict(D=d_tensors, G=g_tensors)
        return self._maybe_with_summary(losses, tensors, with_summary)

    def _add_summaries(self):
        super(OrbitalGAN, self)._add_summaries()
        with K.name_scope('summary/D'):
            tf.summary.scalar('origin_norm', self.origin_norm)
            tf.summary.scalar('angle_loss', self.angle_loss)
            for index, mean in enumerate(self.angle_means):
                tf.summary.scalar('angle-mean-{0}'.format(index),
                                  tf.norm(self.ema.average(mean)))
            for index, variance in enumerate(self.angle_variances):
                tf.summary.scalar('angle-variance-{0}'.format(index),
                                  tf.norm(self.ema.average(variance)))
