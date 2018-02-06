import numpy as np
import tensorflow as tf

from eventgen import CEvent
from nets.net import Net
from nets.utils import copy_net_op, prep_data_cells, prep_data_grids


class QNet(Net):
    def __init__(self, name="QNet", *args, **kwargs):
        """
        Lagging QNet. If 'max_next_action', perform greedy
        Q-learning updates, else SARSA updates.
        """
        super().__init__(name=name, *args, **kwargs)
        self.sess.run(self.copy_online_to_target)

    def _build_net(self, grid, cell, name):
        base_net = self._build_base_net(grid, cell, name)
        with tf.variable_scope('model/' + name) as scope:
            h1 = tf.layers.dense(
                inputs=base_net,
                units=490,
                kernel_initializer=self.kern_init_dense(),
                use_bias=False,
                activation=self.act_fn,
                name="h1")
            if self.pp['dueling_qnet']:
                h2 = tf.layers.dense(
                    inputs=base_net,
                    units=490,
                    kernel_initializer=self.kern_init_dense(),
                    use_bias=False,
                    name="h2")
                value = tf.layers.dense(
                    inputs=h1,
                    units=1,
                    kernel_initializer=self.kern_init_dense(),
                    use_bias=False,
                    name="value")
                advantages = tf.layers.dense(
                    inputs=h2,
                    units=self.n_channels,
                    use_bias=False,
                    kernel_initializer=self.kern_init_dense(),
                    name="advantages")
                # Avg. dueling supposedly more stable than max according to paper
                # Max Dueling
                # q_vals = value + (advantages - tf.reduce_max(
                #     advantages, axis=1, keep_dims=True))
                # Average Dueling
                q_vals = value + (
                    advantages - tf.reduce_mean(advantages, axis=1, keep_dims=True))
                if "online" in name:
                    self.advantages = advantages
                if "target" in name:
                    self.value = value
            else:
                q_vals = tf.layers.dense(
                    inputs=base_net,
                    units=self.n_channels,
                    kernel_initializer=self.kern_init_dense(),
                    kernel_regularizer=self.regularizer,
                    use_bias=False,
                    name="q_vals")
            trainable_vars_by_name = self._get_trainable_vars(scope)
        return q_vals, trainable_vars_by_name

    def build(self):
        depth = self.n_channels * 2 if self.pp['grid_split'] else self.n_channels
        gridshape = [None, self.pp['rows'], self.pp['cols'], depth]
        oh_cellshape = [None, self.pp['rows'], self.pp['cols'], 1]  # Onehot
        self.grid = tf.placeholder(shape=gridshape, dtype=tf.bool, name="grid")
        gridf = tf.cast(self.grid, tf.float32)
        self.cell = tf.placeholder(shape=[None, 2], dtype=tf.int32, name="cell")
        self.oh_cell = tf.placeholder(
            shape=oh_cellshape, dtype=tf.float32, name="oh_cell")
        self.action = tf.placeholder(shape=[None], dtype=tf.int32, name="action")
        self.reward = tf.placeholder(shape=[None], dtype=tf.float32, name="reward")
        self.next_grid = tf.placeholder(shape=gridshape, dtype=tf.bool, name="next_grid")
        next_gridf = tf.cast(self.next_grid, tf.float32)
        self.next_oh_cell = tf.placeholder(
            shape=oh_cellshape, dtype=tf.float32, name="next_oh_cell")
        self.next_action = tf.placeholder(
            shape=[None], dtype=tf.int32, name="next_action")

        self.online_q_vals, online_vars = self._build_net(
            gridf, self.oh_cell, name="q_networks/online")
        # Keep separate weights for target Q network
        target_q_vals, target_vars = self._build_net(
            next_gridf, self.next_oh_cell, name="q_networks/target")
        # copy_online_to_target should be called periodically to creep
        # weights in the target Q-network towards the online Q-network
        self.copy_online_to_target = copy_net_op(online_vars, target_vars,
                                                 self.pp['net_creep_tau'])
        self.copy_online_to_target = tf.no_op()

        self.online_elig_q_vals = self.eligible_qvals(self.grid, self.cell,
                                                      self.online_q_vals)
        self.online_inuse_q_vals = self.inuse_qvals(self.grid, self.cell,
                                                    self.online_q_vals)
        # Maximum valued action from online network
        self.online_q_amax = tf.argmax(self.online_q_vals, axis=1, name="online_q_amax")
        # Maximum Q-value for given next state
        # Q-value for given action
        self.online_q_selected = tf.reduce_sum(
            self.online_q_vals * tf.one_hot(self.action, self.n_channels),
            axis=1,
            name="online_q_selected")

        # Target Q-value for given next action
        self.target_q_selected = tf.reduce_sum(
            target_q_vals * tf.one_hot(self.next_action, self.n_channels),
            axis=1,
            name="target_q_selected")
        if self.pp['dueling_qnet']:
            # WHAT?
            self.next_q = tf.squeeze(self.value)
        elif self.pp['train_net']:
            self.next_q = tf.placeholder(shape=[None], dtype=tf.float32, name="qtarget")
        else:
            self.next_q = self.target_q_selected

        self.q_target = self.reward + self.gamma * self.next_q

        # Below we obtain the loss by taking the sum of squares
        # difference between the target and prediction Q values.
        self.loss = tf.losses.mean_squared_error(
            labels=tf.stop_gradient(self.q_target), predictions=self.online_q_selected)
        # # Write out statistics to file
        # with tf.name_scope("summaries"):
        #     tf.summary.scalar("loss", self.loss)
        #     # tf.summary.scalar("grad_norm", grad_norms)
        #     tf.summary.histogram("qvals", self.online_q_vals)
        # self.summaries = tf.summary.merge_all()
        # self.train_writer = tf.summary.FileWriter(self.log_path + '/train',
        #                                           self.sess.graph)
        # self.eval_writer = tf.summary.FileWriter(self.log_path + '/eval')
        return online_vars

    def forward(self, grid, cell, ce_type):
        if self.pp['dueling_qnet']:
            q_vals_op = self.advantages
        else:
            q_vals_op = self.online_q_vals
        q_vals = self.sess.run(
            [q_vals_op],
            feed_dict={
                self.grid: prep_data_grids(grid, split=self.pp['grid_split']),
                self.oh_cell: prep_data_cells(cell)
            },
            options=self.options,
            run_metadata=self.run_metadata)
        q_vals = np.reshape(q_vals, [-1])
        assert q_vals.shape == (self.n_channels, ), f"{q_vals.shape}\n{q_vals}"
        return q_vals

    def forward2(self, grid, cell, ce_type):
        if ce_type == CEvent.END:
            q_vals_op = self.online_inuse_q_vals
        else:
            q_vals_op = self.online_elig_q_vals
        q_vals, elig = self.sess.run(
            [self.online_q_vals, q_vals_op],
            feed_dict={
                self.grid: prep_data_grids(grid, split=self.pp['grid_split']),
                self.cell: [cell],
                self.oh_cell: prep_data_cells(cell)
            },
            options=self.options,
            run_metadata=self.run_metadata)
        q_vals = np.reshape(q_vals, [-1])
        elig = np.reshape(elig, [-1])
        assert q_vals.shape == (self.n_channels, ), f"{q_vals.shape}\n{q_vals}"
        return q_vals, elig

    def backward(self,
                 grids,
                 cells,
                 actions,
                 rewards,
                 next_grids,
                 next_cells,
                 next_actions=None,
                 next_q=None):
        """
        If 'next_actions' are specified, do SARSA update,
        else greedy selection (Q-Learning).
        If 'next_q', do supervised learning.
        """
        data = {
            self.grid: prep_data_grids(grids, self.pp['grid_split']),
            self.oh_cell: prep_data_cells(cells),
            self.action: actions,
            self.reward: rewards,
        }
        if next_q is not None:
            data[self.next_q] = next_q
        else:
            p_next_grids = prep_data_grids(next_grids, self.pp['grid_split'])
            p_next_cells = prep_data_cells(next_cells)
            if next_actions is None:
                # TODO Can't this be done in a single pass
                next_actions = self.sess.run(
                    self.online_q_amax,
                    feed_dict={
                        self.grid: p_next_grids,
                        self.oh_cell: p_next_cells
                    })
            data.update({
                self.next_grid: p_next_grids,
                self.next_oh_cell: p_next_cells,
                self.next_action: next_actions
            })
        _, loss = self.sess.run(
            [self.do_train, self.loss],
            feed_dict=data,
            options=self.options,
            run_metadata=self.run_metadata)
        return loss
