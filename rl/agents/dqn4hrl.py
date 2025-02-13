from __future__ import division
import warnings
import os
from copy import deepcopy

from tensorflow.python.keras import Input, Model, layers
from tensorflow.python.keras.layers import Lambda
from tensorflow.python.keras.callbacks import History
import tensorflow as tf
# import keras.backend as K
# from keras.layers import Lambda, Input, Layer, Dense
from rl.util import WhiteningNormalizer
from rl.core import Agent
from rl.policy import EpsGreedyQPolicy, GreedyQPolicy
from rl.util import *
from rl.callbacks import (
    CallbackList,
    TestLogger,
    TrainEpisodeLogger,
    TrainIntervalLogger,
    Visualizer,
    ModelIntervalCheckpoint,
    FileLogger
)


def mean_q(y_true, y_pred):
    return tf.reduce_mean(tf.reduce_max(y_pred, axis=-1))


class AbstractDQNAgent(Agent):
    """Write me
    """
    def __init__(self, nb_actions, memory, gamma=.99, batch_size=32, nb_steps_warmup=1000,
                 train_interval=1, memory_interval=1, target_model_update=10000,
                 delta_range=None, delta_clip=np.inf, custom_model_objects={}, **kwargs):
        super(AbstractDQNAgent, self).__init__(**kwargs)

        # Soft vs hard target model updates.
        if target_model_update < 0:
            raise ValueError('`target_model_update` must be >= 0.')
        elif target_model_update >= 1:
            # Hard update every `target_model_update` steps.
            target_model_update = int(target_model_update)
        else:
            # Soft update with `(1 - target_model_update) * old + target_model_update * new`.
            target_model_update = float(target_model_update)

        if delta_range is not None:
            warnings.warn('`delta_range` is deprecated. Please use `delta_clip` instead, which takes a single scalar. For now we\'re falling back to `delta_range[1] = {}`'.format(delta_range[1]))
            delta_clip = delta_range[1]

        # Parameters.
        self.nb_actions = nb_actions
        self.gamma = gamma
        self.batch_size = batch_size
        self.nb_steps_warmup = nb_steps_warmup
        self.train_interval = train_interval
        self.memory_interval = memory_interval
        self.target_model_update = target_model_update
        self.delta_clip = delta_clip
        self.custom_model_objects = custom_model_objects

        # Related objects.
        self.memory = memory

        # State.
        self.compiled = False

    def process_state_batch(self, batch):
        batch = np.array(batch)
        if self.processor is None:
            return batch
        return self.processor.process_state_batch(batch)

    def process_reward_batch(self, batch):
        batch = np.array(batch)
        if self.processor is None:
            return batch
        return self.processor.process_reward_batch(batch)

    def compute_batch_q_values(self, state_batch):
        batch = self.process_state_batch(state_batch)
        q_values = self.model.predict_on_batch(batch)
        assert q_values.shape == (len(state_batch), self.nb_actions)
        return q_values

    def compute_q_values(self, state):
        q_values = self.compute_batch_q_values([state]).flatten()
        assert q_values.shape == (self.nb_actions,)
        return q_values

    def get_config(self):
        return {
            'nb_actions': self.nb_actions,
            'gamma': self.gamma,
            'batch_size': self.batch_size,
            'nb_steps_warmup': self.nb_steps_warmup,
            'train_interval': self.train_interval,
            'memory_interval': self.memory_interval,
            'target_model_update': self.target_model_update,
            'delta_clip': self.delta_clip,
            'memory': get_object_config(self.memory),
        }

# An implementation of the DQN agent as described in Mnih (2013) and Mnih (2015).
# http://arxiv.org/pdf/1312.5602.pdf
# http://arxiv.org/abs/1509.06461
class DQNAgent4Hrl(AbstractDQNAgent):
    """
    # Arguments
        model__: A Keras model.
        policy__: A Keras-rl policy that are defined in [policy](https://github.com/keras-rl/keras-rl/blob/master/rl/policy.py).
        test_policy__: A Keras-rl policy.
        enable_double_dqn__: A boolean which enable target network as a second network proposed by van Hasselt et al. to decrease overfitting.
        enable_dueling_dqn__: A boolean which enable dueling architecture proposed by Mnih et al.
        dueling_type__: If `enable_dueling_dqn` is set to `True`, a type of dueling architecture must be chosen which calculate Q(s,a) from V(s) and A(s,a) differently. Note that `avg` is recommanded in the [paper](https://arxiv.org/abs/1511.06581).
            `avg`: Q(s,a;theta) = V(s;theta) + (A(s,a;theta)-Avg_a(A(s,a;theta)))
            `max`: Q(s,a;theta) = V(s;theta) + (A(s,a;theta)-max_a(A(s,a;theta)))
            `naive`: Q(s,a;theta) = V(s;theta) + A(s,a;theta)

    """
    def __init__(self, model, turn_left_agent, go_straight_agent, turn_right_agent, policy=None, test_policy=None, enable_double_dqn=False, enable_dueling_network=False,
                 dueling_type='avg', *args, **kwargs):
        super(DQNAgent4Hrl, self).__init__(*args, **kwargs)

        # Parameters.
        self.enable_double_dqn = enable_double_dqn
        self.enable_dueling_network = enable_dueling_network
        self.dueling_type = dueling_type
        if self.enable_dueling_network:
            # get the second last layer of the model, abandon the last layer
            layer = model.layers[-2]
            nb_action = model.output._keras_shape[-1]
            # layer y has a shape (nb_action+1,)
            # y[:,0] represents V(s;theta)
            # y[:,1:] represents A(s,a;theta)
            y = layers.Dense(nb_action + 1, activation='linear')(layer.output)
            # caculate the Q(s,a;theta)
            # dueling_type == 'avg'
            # Q(s,a;theta) = V(s;theta) + (A(s,a;theta)-Avg_a(A(s,a;theta)))
            # dueling_type == 'max'
            # Q(s,a;theta) = V(s;theta) + (A(s,a;theta)-max_a(A(s,a;theta)))
            # dueling_type == 'naive'
            # Q(s,a;theta) = V(s;theta) + A(s,a;theta)
            if self.dueling_type == 'avg':
                outputlayer = Lambda(lambda a: tf.expand_dims(a[:, 0], -1) + a[:, 1:] - tf.reduce_mean(a[:, 1:], axis=1, keepdims=True), output_shape=(nb_action,))(y)
            elif self.dueling_type == 'max':
                outputlayer = Lambda(lambda a: tf.expand_dims(a[:, 0], -1) + a[:, 1:] - tf.reduce_max(a[:, 1:], axis=1, keepdims=True), output_shape=(nb_action,))(y)
            elif self.dueling_type == 'naive':
                outputlayer = Lambda(lambda a: tf.expand_dims(a[:, 0], -1) + a[:, 1:], output_shape=(nb_action,))(y)
            else:
                assert False, "dueling_type must be one of {'avg','max','naive'}"

            model = Model(inputs=model.input, outputs=outputlayer)

        # Related objects.
        self.model = model
        if policy is None:
            policy = EpsGreedyQPolicy()
        if test_policy is None:
            test_policy = GreedyQPolicy()
        self.policy = policy
        self.test_policy = test_policy

        self.turn_left_agent = turn_left_agent
        self.go_straight_agent = go_straight_agent
        self.turn_right_agent = turn_right_agent

        # State.
        self.reset_states()

    def get_config(self):
        config = super(DQNAgent4Hrl, self).get_config()
        config['enable_double_dqn'] = self.enable_double_dqn
        config['dueling_type'] = self.dueling_type
        config['enable_dueling_network'] = self.enable_dueling_network
        config['model'] = get_object_config(self.model)
        config['policy'] = get_object_config(self.policy)
        config['test_policy'] = get_object_config(self.test_policy)
        if self.compiled:
            config['target_model'] = get_object_config(self.target_model)
        return config

    def compile(self, optimizer, metrics=[]):
        metrics += [mean_q]  # register default metrics

        # We never train the target model, hence we can set the optimizer and loss arbitrarily.
        self.target_model = clone_model(self.model, self.custom_model_objects)
        self.target_model.compile(optimizer='sgd', loss='mse')
        self.model.compile(optimizer='sgd', loss='mse')

        # Compile model.
        if self.target_model_update < 1.:
            # We use the `AdditionalUpdatesOptimizer` to efficiently soft-update the target model.
            updates = get_soft_target_model_updates(self.target_model, self.model, self.target_model_update)
            optimizer = AdditionalUpdatesOptimizer(optimizer, updates)

        def clipped_masked_error(args):
            y_true, y_pred, mask = args
            loss = huber_loss(y_true, y_pred, self.delta_clip)
            loss *= mask  # apply element-wise mask
            return tf.reduce_sum(loss, axis=-1)

        # Create trainable model. The problem is that we need to mask the output since we only
        # ever want to update the Q values for a certain action. The way we achieve this is by
        # using a custom Lambda layer that computes the loss. This gives us the necessary flexibility
        # to mask out certain parameters by passing in multiple inputs to the Lambda layer.
        y_pred = self.model.output
        y_true = Input(name='y_true', shape=(self.nb_actions,))
        mask = Input(name='mask', shape=(self.nb_actions,))
        loss_out = Lambda(clipped_masked_error, output_shape=(1,), name='loss')([y_true, y_pred, mask])
        ins = [self.model.input] if type(self.model.input) is not list else self.model.input
        trainable_model = Model(inputs=ins + [y_true, mask], outputs=[loss_out, y_pred])
        assert len(trainable_model.output_names) == 2
        combined_metrics = {trainable_model.output_names[1]: metrics}
        losses = [  # https://www.imooc.com/article/details/id/31034 for explanation
            lambda y_true, y_pred: y_pred,  # loss is computed in Lambda layer
            lambda y_true, y_pred: tf.zeros_like(y_pred),  # we only include this for the metrics
        ]
        trainable_model.compile(optimizer=optimizer, loss=losses, metrics=combined_metrics)
        self.trainable_model = trainable_model

        self.compiled = True

    def load_weights(self, filepath):
        # load models weights
        self.model.load_weights(filepath)
        self.update_target_model_hard()
        filename, extension = os.path.splitext(filepath)
        left_model_filepath = filename + '_left_model' + extension
        straight_model_filepath = filename + '_straight_model' + extension
        right_model_filepath = filename + '_right_model' + extension
        self.turn_left_agent.load_weights(left_model_filepath)
        self.go_straight_agent.load_weights(straight_model_filepath)
        self.turn_right_agent.load_weights(right_model_filepath)
        # load state processor
        upper_processor_filepath = filename + '.pickle'
        left_processor_filepath = filename + '_left_model' + '.pickle'
        straight_processor_filepath = filename + '_straight_model' + '.pickle'
        right_processor_filepath = filename + '_right_model' + '.pickle'
        if not self.processor.normalizer:
            self.processor.normalizer = WhiteningNormalizer(shape=(10, 56))
        if not self.turn_left_agent.processor.normalizer:
            self.turn_left_agent.processor.normalizer = WhiteningNormalizer(shape=(10, 41))
        if not self.go_straight_agent.processor.normalizer:
            self.go_straight_agent.processor.normalizer = WhiteningNormalizer(shape=(10, 59))
        if not self.turn_right_agent.processor.normalizer:
            self.turn_right_agent.processor.normalizer = WhiteningNormalizer(shape=(10, 41))
        self.processor.normalizer.load_param(upper_processor_filepath)
        self.turn_left_agent.processor.normalizer.load_param(left_processor_filepath)
        self.go_straight_agent.processor.normalizer.load_param(straight_processor_filepath)
        self.turn_right_agent.processor.normalizer.load_param(right_processor_filepath)

    def save_weights(self, filepath, overwrite=True):
        # save models weights
        self.model.save_weights(filepath, overwrite=overwrite)
        filename, extension = os.path.splitext(filepath)
        left_model_filepath = filename + '_left_model' + extension
        straight_model_filepath = filename + '_straight_model' + extension
        right_model_filepath = filename + '_right_model' + extension
        self.turn_left_agent.save_weights(left_model_filepath, overwrite=overwrite)
        self.go_straight_agent.save_weights(straight_model_filepath, overwrite=overwrite)
        self.turn_right_agent.save_weights(right_model_filepath, overwrite=overwrite)
        # save state processor
        upper_processor_filepath = filename + '.pickle'
        left_processor_filepath = filename + '_left_model' + '.pickle'
        straight_processor_filepath = filename + '_straight_model' + '.pickle'
        right_processor_filepath = filename + '_right_model' + '.pickle'
        if self.processor.normalizer:
            self.processor.normalizer.save_param(upper_processor_filepath)
        if self.turn_left_agent.processor.normalizer:
            self.turn_left_agent.processor.normalizer.save_param(left_processor_filepath)
        if self.go_straight_agent.processor.normalizer:
            self.go_straight_agent.processor.normalizer.save_param(straight_processor_filepath)
        if self.turn_right_agent.processor.normalizer:
            self.turn_right_agent.processor.normalizer.save_param(right_processor_filepath)

    def reset_states(self):
        self.recent_action = None
        self.recent_observation = None
        if self.compiled:
            self.model.reset_states()
            self.target_model.reset_states()
        self.turn_left_agent.reset_states()
        self.go_straight_agent.reset_states()
        self.turn_right_agent.reset_states()

    def update_target_model_hard(self):
        self.target_model.set_weights(self.model.get_weights())

    def forward(self, observation):  # observation = [timesteps, features]
        # Select an action.
        state = self.memory.get_recent_state(observation)  # TODO
        q_values = self.compute_q_values(state)
        if self.training:
            upper_action = self.policy.select_action(q_values=q_values)
        else:
            upper_action = self.test_policy.select_action(q_values=q_values)

        if upper_action == 0:  # left
            left_obs = np.column_stack((observation[:, :30], observation[:, -8:], np.tile(np.array([1, 0, 0]), (observation.shape[0], 1)))) # 30 + 8 + 3 = 41
            lower_action = self.turn_left_agent.forward(left_obs)  # lower_action = [goal_delta_x, acc]
        elif upper_action == 1:  # go_straight
            straight_obs = np.column_stack((deepcopy(observation), np.tile(np.array([0, 1, 0]), (observation.shape[0], 1))))  # 56 + 3 = 59
            lower_action = self.go_straight_agent.forward(straight_obs)
        else:
            right_obs = np.column_stack((observation[:, 18:], np.tile(np.array([0, 0, 1]), (observation.shape[0], 1))))  # 56- 18 + 3 = 41
            lower_action = self.turn_right_agent.forward(right_obs)

        # Book-keeping.
        self.recent_observation = observation
        self.recent_action = upper_action

        return [upper_action, lower_action[0], lower_action[1]]

    def backward(self, reward, terminal):
        # Store most recent experience in memory.
        if self.step % self.memory_interval == 0:
            self.memory.append(self.recent_observation, self.recent_action, reward, terminal,
                               training=self.training)
            if self.recent_action == 0:
                self.turn_left_agent.memory.append(self.turn_left_agent.recent_observation,
                                                   self.turn_left_agent.recent_action, reward, 1,
                                                   training=self.training)
            elif self.recent_action == 1:
                self.go_straight_agent.memory.append(self.go_straight_agent.recent_observation,
                                                     self.go_straight_agent.recent_action, reward, 1,
                                                     training=self.training)
            else:
                self.turn_right_agent.memory.append(self.turn_right_agent.recent_observation,
                                                    self.turn_right_agent.recent_action, reward, 1,
                                                    training=self.training)

        metrics = [np.nan for _ in self.metrics_names]
        if not self.training:
            # We're done here. No need to update the experience memory since we only use the working
            # memory to obtain the state over the most recent observations.
            return metrics

        # Train the network on a single stochastic batch.
        if self.step > self.nb_steps_warmup and self.step % self.train_interval == 0:
            left_metrics = self.turn_left_agent.backward(0, 0)  # these parameters have no use
            straight_metrics = self.go_straight_agent.backward(0, 0)
            right_metrics = self.turn_right_agent.backward(0, 0)
            experiences = self.memory.sample(self.batch_size)
            assert len(experiences) == self.batch_size

            # Start by extracting the necessary parameters (we use a vectorized implementation).
            state0_batch = []
            reward_batch = []
            action_batch = []
            terminal1_batch = []
            state1_batch = []
            for e in experiences:
                state0_batch.append(e.state0)
                state1_batch.append(e.state1)
                reward_batch.append(e.reward)
                action_batch.append(e.action)
                terminal1_batch.append(0. if e.terminal1 else 1.)

            # Prepare and validate parameters.
            state0_batch = self.process_state_batch(state0_batch)
            state1_batch = self.process_state_batch(state1_batch)
            terminal1_batch = np.array(terminal1_batch)
            reward_batch = self.process_reward_batch(reward_batch)
            assert reward_batch.shape == (self.batch_size,)
            assert terminal1_batch.shape == reward_batch.shape
            assert len(action_batch) == len(reward_batch)

            # Compute Q values for mini-batch update.
            if self.enable_double_dqn:
                # According to the paper "Deep Reinforcement Learning with Double Q-learning"
                # (van Hasselt et al., 2015), in Double DQN, the online network predicts the actions
                # while the target network is used to estimate the Q value.
                q_values = self.model.predict_on_batch(state1_batch)
                assert q_values.shape == (self.batch_size, self.nb_actions)
                actions = np.argmax(q_values, axis=1)
                assert actions.shape == (self.batch_size,)

                # Now, estimate Q values using the target network but select the values with the
                # highest Q value wrt to the online model (as computed above).
                target_q_values = self.target_model.predict_on_batch(state1_batch)
                assert target_q_values.shape == (self.batch_size, self.nb_actions)
                q_batch = target_q_values[range(self.batch_size), actions]
            else:
                # Compute the q_values given state1, and extract the maximum for each sample in the batch.
                # We perform this prediction on the target_model instead of the model for reasons
                # outlined in Mnih (2015). In short: it makes the algorithm more stable.
                target_q_values = self.target_model.predict_on_batch(state1_batch)
                assert target_q_values.shape == (self.batch_size, self.nb_actions)
                q_batch = np.max(target_q_values, axis=1).flatten()
            assert q_batch.shape == (self.batch_size,)

            targets = np.zeros((self.batch_size, self.nb_actions))
            dummy_targets = np.zeros((self.batch_size,))
            masks = np.zeros((self.batch_size, self.nb_actions))

            # Compute r_t + gamma * max_a Q(s_t+1, a) and update the target targets accordingly,
            # but only for the affected output units (as given by action_batch).
            discounted_reward_batch = self.gamma * q_batch
            # Set discounted reward to zero for all states that were terminal.
            discounted_reward_batch *= terminal1_batch
            assert discounted_reward_batch.shape == reward_batch.shape
            Rs = reward_batch + discounted_reward_batch
            for idx, (target, mask, R, action) in enumerate(zip(targets, masks, Rs, action_batch)):
                target[action] = R  # update action with estimated accumulated reward
                dummy_targets[idx] = R
                mask[action] = 1.  # enable loss for this specific action
            targets = np.array(targets).astype('float32')
            masks = np.array(masks).astype('float32')

            # Finally, perform a single update on the entire batch. We use a dummy target since
            # the actual loss is computed in a Lambda layer that needs more complex input. However,
            # it is still useful to know the actual target to compute metrics properly.
            ins = [state0_batch] if type(self.model.input) is not list else state0_batch
            metrics = self.trainable_model.train_on_batch(ins + [targets, masks], [dummy_targets, targets])
            metrics = [metric for idx, metric in enumerate(metrics) if idx not in (1, 2)]  # throw away individual losses
            metrics += self.policy.metrics
            if self.processor is not None:
                metrics += self.processor.metrics
            metrics = metrics + left_metrics + straight_metrics + right_metrics

        if self.target_model_update >= 1 and self.step % self.target_model_update == 0:
            self.update_target_model_hard()

        return metrics

    def fit_hrl(self, env, nb_steps, random_start_step_policy, callbacks=None, verbose=1,
            visualize=False, pre_warm_steps=0, log_interval=100, save_interval=1,
            nb_max_episode_steps=None):

        if not self.compiled:
            raise RuntimeError('Your tried to fit your agent but it hasn\'t been'
                               ' compiled yet. Please call `compile()` before `fit()`.')

        self.training = True
        self.turn_left_agent.training = True
        self.go_straight_agent.training = True
        self.turn_right_agent.training = True

        callbacks = [] if not callbacks else callbacks[:]

        if verbose == 1:
            callbacks += [TrainIntervalLogger(interval=log_interval)]
        elif verbose > 1:
            callbacks += [TrainEpisodeLogger()]
        if visualize:
            callbacks += [Visualizer()]

        parent_dir = os.path.dirname(os.path.dirname(__file__))
        callbacks += [FileLogger(filepath=parent_dir + os.sep + 'log.json')]
        callbacks += [ModelIntervalCheckpoint(filepath=parent_dir + '/checkpoints/model_step{step}.h5f',
                                              interval=save_interval,
                                              verbose=1)]
        history = History()
        callbacks += [history]
        callbacks = CallbackList(callbacks)
        callbacks.set_model(self)
        callbacks._set_env(env)
        params = {
            'nb_steps': nb_steps,
        }
        callbacks.set_params(params)
        self._on_train_begin()
        callbacks.on_train_begin()

        episode = np.int16(0)
        self.step = np.int16(0)
        self.turn_left_agent.step = np.int16(0)
        self.go_straight_agent.step = np.int16(0)
        self.turn_right_agent.step = np.int16(0)
        observation = env.encoded_obs
        episode_reward = None
        episode_step = None
        did_abort = False

        # warm steps
        print('pre warming up:')
        for _ in range(pre_warm_steps):
            normed_action= random_start_step_policy()
            recent_action = normed_action
            recent_observation = observation  # put in normed action and unprocessed observation
            action = self.processor.process_action(recent_action)  # [0/1/2, goal_delta_x, acc]

            callbacks.on_action_begin(action)
            observation, reward, done, info = env.step(action)
            observation = deepcopy(observation)
            if self.processor is not None:
                observation, reward, done, info = self.processor.process_step(observation, reward, done, info)
            callbacks.on_action_end(action)

            self.memory.append(recent_observation, recent_action[0], reward, done,
                               training=self.training)
            if recent_action[0] == 0:
                left_obs = np.column_stack((recent_observation[:, :30], recent_observation[:, -8:], np.tile(np.array([1, 0, 0]), (
                    recent_observation.shape[0], 1))))  # 30 + 8 + 3 = 41
                lower_action = recent_action[1:]
                self.turn_left_agent.memory.append(left_obs, lower_action, reward, 1,
                                                   training=self.training)
            elif recent_action[0] == 1:
                straight_obs = np.column_stack((deepcopy(recent_observation), np.tile(np.array([0, 1, 0]),
                                                                      (recent_observation.shape[0], 1)))) # 56 + 3 = 59
                lower_action = recent_action[1:]
                self.go_straight_agent.memory.append(straight_obs, lower_action, reward, 1,
                                                     training=self.training)
            else:
                right_obs = np.column_stack((recent_observation[:, 18:], np.tile(np.array([0, 0, 1]),
                                                                 (recent_observation.shape[0], 1))))  # 56- 18 + 3 = 41
                lower_action = recent_action[1:]
                self.turn_right_agent.memory.append(right_obs, lower_action, reward, 1,
                                                    training=self.training)
            print('————————————————————————————————————————')
            print({'upper_memory_len: ': self.memory.nb_entries,
                   'left_memory_len: ': self.turn_left_agent.memory.nb_entries,
                   'straight_memory_len: ': self.go_straight_agent.memory.nb_entries,
                   'right_memory_len: ': self.turn_right_agent.memory.nb_entries})
            print('————————————————————————————————————————')
            # TODO: always has a point is not done, but there would be only one bad point in the buffer
            if done:
                def random_init_state(flag=True):
                    init_state = [-800, -150 - 3.75 * 5 / 2, 5, 0]
                    if flag:
                        x = np.random.random() * 1000 - 800
                        lane = np.random.choice([0, 1, 2, 3])
                        y_fn = lambda lane: \
                        [-150 - 3.75 * 7 / 2, -150 - 3.75 * 5 / 2, -150 - 3.75 * 3 / 2, -150 - 3.75 * 1 / 2][lane]
                        y = y_fn(lane)
                        v = np.random.random() * 25
                        heading = 0
                        init_state = [x, y, v, heading]
                    return init_state

                observation = deepcopy(env.reset(init_state=random_init_state(flag=True)))
                if self.processor is not None:
                    observation = self.processor.process_observation(observation)

        observation = None

        try:
            while self.step < nb_steps:
                if observation is None:  # start of a new episode
                    callbacks.on_episode_begin(episode)
                    episode_step = np.int16(0)
                    episode_reward = np.float32(0)

                    # Obtain the initial observation by resetting the environment.
                    self.reset_states()

                    def random_init_state(flag=True):
                        init_state = [-800, -150-3.75*5/2, 5, 0]
                        if flag:
                            x = np.random.uniform(0, 1) * 1000 - 800
                            lane = np.random.choice([0, 1, 2, 3])
                            y_fn = lambda lane: [-150-3.75*7/2, -150-3.75*5/2, -150-3.75*3/2, -150-3.75*1/2][lane]
                            y = y_fn(lane)
                            v = np.random.uniform(0, 1) * 25
                            heading = 0
                            init_state = [x, y, v, heading]
                        return init_state

                    observation = deepcopy(env.reset(init_state=random_init_state()))
                    if self.processor is not None:
                        observation = self.processor.process_observation(observation)
                    assert observation is not None

                # At this point, we expect to be fully initialized.
                assert episode_reward is not None
                assert episode_step is not None
                assert observation is not None

                # Run a single step.
                callbacks.on_step_begin(episode_step)
                # This is were all of the work happens. We first perceive and compute the action
                # (forward step) and then use the reward to improve (backward step).
                action = self.forward(observation)  # this is normed action
                action = self.processor.process_action(action)  # this is processed action for env
                done = False

                callbacks.on_action_begin(action)
                observation, reward, done, info = env.step(action)
                observation = deepcopy(observation)
                if self.processor is not None:
                    observation, reward, done, info = self.processor.process_step(observation, reward, done, info)
                callbacks.on_action_end(action)

                if nb_max_episode_steps and episode_step >= nb_max_episode_steps - 1:
                    # Force a terminal state.
                    done = True
                metrics = self.backward(reward, terminal=done)
                episode_reward += reward
                step_logs = {
                    'action': action,  # processed action
                    'observation': observation,  # true obs
                    'reward': reward,
                    'metrics': metrics,
                    'episode': episode
                    # 'info': info,
                }

                callbacks.on_step_end(episode_step, step_logs)
                episode_step += 1
                self.step += 1
                self.turn_left_agent.step += 1
                self.go_straight_agent.step += 1
                self.turn_right_agent.step += 1

                memory_len = [self.turn_left_agent.memory.nb_entries, self.go_straight_agent.memory.nb_entries,
                              self.turn_right_agent.memory.nb_entries]

                if done:
                    episode_logs = {
                        'episode_reward': episode_reward,
                        'nb_episode_steps': episode_step,
                        'nb_steps': self.step,
                        'memory_len': memory_len
                    }
                    callbacks.on_episode_end(episode, episode_logs)

                    episode += 1
                    observation = None
                    episode_step = None
                    episode_reward = None
        except KeyboardInterrupt:
            # We catch keyboard interrupts here so that training can be be safely aborted.
            # This is so common that we've built this right into this function, which ensures that
            # the `on_train_end` method is properly called.
            did_abort = True
        callbacks.on_train_end(logs={'did_abort': did_abort})
        self._on_train_end()

        return history

    def test_hrl(self, env, nb_episodes=1, callbacks=None, visualize=True,
             nb_max_episode_steps=None, verbose=2, model_path=None):

        if model_path is not None:
            self.load_weights(model_path)
        if not self.compiled:
            raise RuntimeError('Your tried to test your agent but it hasn\'t been '
                               'compiled yet. Please call `compile()` before `test()`.')

        self.training = False
        self.turn_left_agent.training = False
        self.go_straight_agent.training = False
        self.turn_right_agent.training = False
        self.step = np.int16(0)
        self.turn_left_agent.step = np.int16(0)
        self.go_straight_agent.step = np.int16(0)
        self.turn_right_agent.step = np.int16(0)

        callbacks = [] if not callbacks else callbacks[:]

        if verbose >= 1:
            callbacks += [TestLogger()]
        if visualize:
            callbacks += [Visualizer()]
        history = History()
        callbacks += [history]
        callbacks = CallbackList(callbacks)
        callbacks.set_model(self)
        callbacks._set_env(env)
        params = {
            'nb_episodes': nb_episodes,
        }
        callbacks.set_params(params)
        self._on_test_begin()
        callbacks.on_train_begin()
        for episode in range(nb_episodes):
            callbacks.on_episode_begin(episode)
            episode_reward = 0.
            episode_step = 0

            # Obtain the initial observation by resetting the environment.
            self.reset_states()

            def random_init_state(flag=True):
                init_state = [-800, -150 - 3.75 * 5 / 2, 5, 0]
                if flag:
                    x = np.random.random() * 1000 - 800
                    lane = np.random.choice([0, 1, 2, 3])
                    y_fn = lambda lane: \
                    [-150 - 3.75 * 7 / 2, -150 - 3.75 * 5 / 2, -150 - 3.75 * 3 / 2, -150 - 3.75 * 1 / 2][lane]
                    y = y_fn(lane)
                    v = np.random.random() * 25
                    heading = 0
                    init_state = [x, y, v, heading]
                return init_state

            observation = deepcopy(env.reset(init_state=random_init_state(flag=True)))
            assert observation is not None

            # Run the episode until we're done.
            done = False
            while not done:
                callbacks.on_step_begin(episode_step)

                action = self.forward(observation)
                action = self.processor.process_action(action)
                reward = 0.
                callbacks.on_action_begin(action)
                observation, reward, done, info = env.step(action)
                observation = deepcopy(observation)
                callbacks.on_action_end(action)
                if nb_max_episode_steps and episode_step >= nb_max_episode_steps - 1:
                    done = True
                self.backward(reward, terminal=done)
                episode_reward += reward

                step_logs = {
                    'action': action,
                    'observation': observation,
                    'reward': reward,
                    'episode': episode
                }
                callbacks.on_step_end(episode_step, step_logs)
                episode_step += 1
                self.step += 1
                self.turn_left_agent.step += 1
                self.go_straight_agent.step += 1
                self.turn_right_agent.step += 1

            # We are in a terminal state but the agent hasn't yet seen it. We therefore
            # perform one more forward-backward call and simply ignore the action before
            # resetting the environment. We need to pass in `terminal=False` here since
            # the *next* state, that is the state of the newly reset environment, is
            # always non-terminal by convention.
            self.forward(observation)
            self.backward(0., terminal=False)

            # Report end of episode.
            episode_logs = {
                'episode_reward': episode_reward,
                'nb_steps': episode_step,
            }
            callbacks.on_episode_end(episode, episode_logs)
        callbacks.on_train_end()
        self._on_test_end()

        return history

    @property
    def layers(self):
        return self.model.layers[:]

    @property
    def metrics_names(self):
        # Throw away individual losses and replace output name since this is hidden from the user.
        assert len(self.trainable_model.output_names) == 2
        dummy_output_name = self.trainable_model.output_names[1]
        model_metrics = [name for idx, name in enumerate(self.trainable_model.metrics_names) if idx not in (1, 2)]
        model_metrics = [name.replace(dummy_output_name + '_', '') for name in model_metrics]

        names = model_metrics + self.policy.metrics_names[:]
        if self.processor is not None:
            names += self.processor.metrics_names[:]
        left_model_metrics = ['left_' + name for name in self.turn_left_agent.metrics_names]
        straight_model_metrics = ['straight_' + name for name in self.go_straight_agent.metrics_names]
        right_model_metrics = ['right_' + name for name in self.turn_right_agent.metrics_names]
        return names + left_model_metrics + straight_model_metrics + right_model_metrics

    @property
    def policy(self):
        return self.__policy

    @policy.setter
    def policy(self, policy):
        self.__policy = policy
        self.__policy._set_agent(self)

    @property
    def test_policy(self):
        return self.__test_policy

    @test_policy.setter
    def test_policy(self, policy):
        self.__test_policy = policy
        self.__test_policy._set_agent(self)

