import collections

import keras.backend as K
import keras.losses
import numpy as np
import tensorflow as tf
from keras.layers import (Activation, Concatenate, Conv2D, Dense, Flatten,
                          Input, LeakyReLU, Reshape, UpSampling2D)
from keras.models import Model

from cytogan.extra.layers import AddNoise, BatchNorm, RandomNormal
from cytogan.models import gan

Hyper = collections.namedtuple('Hyper', [
    'image_shape',
    'generator_filters',
    'discriminator_filters',
    'generator_strides',
    'discriminator_strides',
    'latent_size',
    'noise_size',
    'initial_shape',
    'conditional_shape',
])


def smooth_labels(labels, low=0.8, high=1.0):
    with K.name_scope('noisy_labels'):
        return labels * tf.random_uniform(tf.shape(labels), low, high)


def _conditional_input(conditional_shape):
    if conditional_shape is None:
        return None
    return Input(shape=conditional_shape, name='conditional')


def mix_images_with_variables(images, variables):
    image_shape = list(map(int, images.shape[1:]))
    flat_size = np.prod(image_shape)
    new_shape = image_shape[:-1] + [image_shape[-1] * 2]

    with K.name_scope('mix_images_w_vars'):
        flat_images = Reshape([flat_size])(images)
        vectors = Concatenate(axis=1)([flat_images, variables])
        mix = Dense(flat_size * 2)(vectors)
        mix = LeakyReLU(alpha=0.2)(mix)
        volume = Reshape(new_shape)(mix)

    return volume


class DCGAN(gan.GAN):
    def __init__(self, hyper, learning, session):
        self.labels = None  # 0/1
        self.d_final = None  # D(x)

        super(DCGAN, self).__init__(hyper, learning, session)

    def _train_discriminator(self, fake_images, real_images, conditional,
                             with_summary):
        labels = np.concatenate(
            [np.zeros(len(fake_images)),
             np.ones(len(real_images))], axis=0)
        images = np.concatenate([fake_images, real_images], axis=0)
        fetches = [self.optimizer['D'], self.loss['D']]
        if with_summary:
            fetches.append(self.discriminator_summary)

        feed_dict = {
            self.batch_size: [len(fake_images)],
            self.images: images,
            self.labels: labels,
            K.learning_phase(): 1,
        }
        if self.discriminator_conditional is not None:
            # Not sure why we need to feed the generator conditional, but TF
            # complains otherwise (same with batch_size above).
            feed_dict[self.generator_conditional] = np.zeros_like(conditional)
            # Duplicate the conditional (for the real and for the fake images).
            conditional = np.concatenate([conditional, conditional], axis=0)
            feed_dict[self.discriminator_conditional] = conditional

        return self.session.run(fetches, feed_dict)[1:]

    def _train_generator(self, batch_size, conditional, with_summary):
        fetches = [self.optimizer['G'], self.loss['G']]
        if with_summary:
            fetches.append(self.generator_summary)

        feed_dict = {self.batch_size: [batch_size], K.learning_phase(): 1}
        if self.generator_conditional is not None:
            feed_dict[self.generator_conditional] = conditional

        return self.session.run(fetches, feed_dict)[1:]

    def _define_graph(self):
        with K.name_scope('G'):
            self.batch_size = Input(batch_shape=[1], name='batch_size')
            self.noise = RandomNormal(self.noise_size)(self.batch_size)
            self.generator_conditional = _conditional_input(
                self.conditional_shape)
            self.fake_images = self._define_generator(
                self.noise, self.generator_conditional)

        with K.name_scope('D'):
            self.images = Input(shape=self.image_shape, name='images')
            self.discriminator_conditional = _conditional_input(
                self.conditional_shape)
            logits = self._define_discriminator(self.images,
                                                self.discriminator_conditional)

            self.latent = Dense(self.latent_size, name='latent')(logits)
            self.d_final = self._define_final_discriminator_layer(self.latent)

        self.labels = Input(batch_shape=[None], name='labels')

        generator_inputs = [self.batch_size]
        discriminator_inputs = [self.images]
        generator_outputs = [self.fake_images]
        if self.is_conditional:
            generator_inputs.append(self.generator_conditional)
            generator_outputs.append(self.generator_conditional)
            discriminator_inputs.append(self.discriminator_conditional)

        self.generator = Model(generator_inputs, self.fake_images, name='G')
        self.discriminator = Model(
            discriminator_inputs, self.d_final, name='D')
        self.encoder = Model(discriminator_inputs, self.latent, name='E')
        self.gan = Model(
            generator_inputs,
            self.discriminator(generator_outputs),
            name=self.name)

        self.loss = dict(
            D=self._define_discriminator_loss(self.labels, self.d_final),
            G=self._define_generator_loss(self.gan.outputs[0]))

    def _define_generator(self, noise, conditional=None):
        if conditional is None:
            logits = noise
        else:
            logits = Concatenate(axis=1)([noise, conditional])
        first_filter = self.generator_filters[0]
        G = Dense(np.prod(self.initial_shape) * first_filter)(logits)
        G = BatchNorm()(G)
        G = LeakyReLU(alpha=0.2)(G)
        G = Reshape(self.initial_shape + self.generator_filters[:1])(G)

        for filters, stride in zip(self.generator_filters[1:],
                                   self.generator_strides[1:]):
            if stride > 1:
                G = UpSampling2D(stride)(G)
            G = Conv2D(filters, (5, 5), padding='same')(G)
            G = BatchNorm()(G)
            G = LeakyReLU(alpha=0.2)(G)

        G = Conv2D(self.number_of_channels, (5, 5), padding='same')(G)
        G = Activation('tanh')(G)
        assert G.shape[1:] == self.image_shape, G.shape

        return G

    def _define_discriminator(self, images, conditional=None):
        noisy_images = AddNoise()(images)
        if conditional is None:
            D = noisy_images
        else:
            D = mix_images_with_variables(noisy_images, conditional)
        for filters, stride in zip(self.discriminator_filters,
                                   self.discriminator_strides):
            D = Conv2D(
                filters, (5, 5), strides=(stride, stride), padding='same')(D)
            D = LeakyReLU(alpha=0.2)(D)
        D = Flatten()(D)

        return D

    def _define_generator_loss(self, probability):
        with K.name_scope('G_loss'):
            probability = K.squeeze(probability, 1)
            ones = K.ones_like(probability)
            return keras.losses.binary_crossentropy(ones, probability)

    def _define_discriminator_loss(self, labels, probability):
        noisy_labels = smooth_labels(labels)
        with K.name_scope('D_loss'):
            probability = K.squeeze(probability, 1)
            return keras.losses.binary_crossentropy(noisy_labels, probability)

    def _define_final_discriminator_layer(self, latent):
        return Dense(1, activation='sigmoid', name='Probability')(latent)

    def _add_summaries(self):
        with K.name_scope('summaries/G'):
            tf.summary.histogram('noise', self.noise)
            tf.summary.scalar('G_loss', self.loss['G'])
            tf.summary.image(
                'generated_images', self.fake_images, max_outputs=8)
            if self.generator_conditional is not None:
                tf.summary.histogram('conditional', self.generator_conditional)

        with K.name_scope('summaries/D'):
            tf.summary.histogram('latent', self.latent)
            tf.summary.scalar('D_loss', self.loss['D'])
            batch_size = tf.cast(tf.squeeze(self.batch_size), tf.int32)
            fake_probability = self.d_final[:batch_size]
            real_probability = self.d_final[batch_size:]
            tf.summary.histogram('fake_output', fake_probability)
            tf.summary.histogram('real_output', real_probability)
            if self.discriminator_conditional is not None:
                tf.summary.histogram('conditional',
                                     self.discriminator_conditional)
