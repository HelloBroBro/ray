import gymnasium as gym
from gymnasium.spaces import Box, Discrete
import json
import numpy as np
import os
import random
import tempfile
import time
import unittest

import ray
from ray.rllib.algorithms.algorithm_config import AlgorithmConfig
from ray.rllib.algorithms.ppo import PPOConfig
from ray.rllib.env.multi_agent_env import MultiAgentEnv
from ray.rllib.evaluation.rollout_worker import RolloutWorker
from ray.rllib.evaluation.metrics import collect_metrics
from ray.rllib.evaluation.postprocessing import compute_advantages
from ray.rllib.evaluation.worker_set import WorkerSet
from ray.rllib.examples.envs.classes.mock_env import (
    MockEnv,
    MockEnv2,
    MockVectorEnv,
    VectorizedMockEnv,
)
from ray.rllib.examples.envs.classes.multi_agent import MultiAgentCartPole
from ray.rllib.examples.envs.classes.random_env import RandomEnv
from ray.rllib.examples._old_api_stack.policy.random_policy import RandomPolicy
from ray.rllib.offline.dataset_reader import DatasetReader, get_dataset_and_shards
from ray.rllib.offline.json_reader import JsonReader
from ray.rllib.policy.policy import Policy, PolicySpec
from ray.rllib.policy.sample_batch import (
    DEFAULT_POLICY_ID,
    MultiAgentBatch,
    SampleBatch,
    convert_ma_batch_to_sample_batch,
)
from ray.rllib.utils.annotations import override
from ray.rllib.utils.metrics import NUM_AGENT_STEPS_SAMPLED, NUM_AGENT_STEPS_TRAINED
from ray.rllib.utils.test_utils import check, framework_iterator
from ray.tune.registry import register_env


class MockPolicy(RandomPolicy):
    @override(RandomPolicy)
    def compute_actions(
        self,
        obs_batch,
        state_batches=None,
        prev_action_batch=None,
        prev_reward_batch=None,
        episodes=None,
        explore=None,
        timestep=None,
        **kwargs
    ):
        return np.array([random.choice([0, 1])] * len(obs_batch)), [], {}

    @override(Policy)
    def postprocess_trajectory(self, batch, other_agent_batches=None, episode=None):
        assert episode is not None
        super().postprocess_trajectory(batch, other_agent_batches, episode)
        return compute_advantages(batch, 100.0, 0.9, use_gae=False, use_critic=False)


class BadPolicy(RandomPolicy):
    @override(RandomPolicy)
    def compute_actions(
        self,
        obs_batch,
        state_batches=None,
        prev_action_batch=None,
        prev_reward_batch=None,
        episodes=None,
        explore=None,
        timestep=None,
        **kwargs
    ):
        raise Exception("intentional error")


class FailOnStepEnv(gym.Env):
    def __init__(self):
        self.observation_space = gym.spaces.Discrete(1)
        self.action_space = gym.spaces.Discrete(2)

    def reset(self, *, seed=None, options=None):
        raise ValueError("kaboom")

    def step(self, action):
        raise ValueError("kaboom")


class TestRolloutWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init(num_cpus=5)

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()

    def test_basic(self):
        ev = RolloutWorker(
            env_creator=lambda _: gym.make("CartPole-v1"),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(num_env_runners=0),
        )
        batch = convert_ma_batch_to_sample_batch(ev.sample())
        for key in [
            "obs",
            "actions",
            "rewards",
            "terminateds",
            "terminateds",
            "advantages",
            "prev_rewards",
            "prev_actions",
        ]:
            self.assertIn(key, batch)
            self.assertGreater(np.abs(np.mean(batch[key])), 0)

        # Our MockPolicy should never reach a full truncated episode.
        # Expect all truncateds flags to be False.
        self.assertEqual(np.abs(np.mean(batch["truncateds"])), 0.0)

        def to_prev(vec):
            out = np.zeros_like(vec)
            for i, v in enumerate(vec):
                if i + 1 < len(out) and not batch["terminateds"][i]:
                    out[i + 1] = v
            return out.tolist()

        self.assertEqual(batch["prev_rewards"].tolist(), to_prev(batch["rewards"]))
        self.assertEqual(batch["prev_actions"].tolist(), to_prev(batch["actions"]))
        self.assertGreater(batch["advantages"][0], 1)
        ev.stop()

    def test_batch_ids(self):
        fragment_len = 100
        ev = RolloutWorker(
            env_creator=lambda _: gym.make("CartPole-v1"),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=fragment_len, num_env_runners=0
            ),
        )
        batch1 = convert_ma_batch_to_sample_batch(ev.sample())
        batch2 = convert_ma_batch_to_sample_batch(ev.sample())
        unroll_ids_1 = set(batch1["unroll_id"])
        unroll_ids_2 = set(batch2["unroll_id"])
        # Assert no overlap of unroll IDs between sample() calls.
        self.assertTrue(not any(uid in unroll_ids_2 for uid in unroll_ids_1))
        # CartPole episodes should be short initially: Expect more than one
        # unroll ID in each batch.
        self.assertTrue(len(unroll_ids_1) > 1)
        self.assertTrue(len(unroll_ids_2) > 1)
        ev.stop()

    def test_global_vars_update(self):
        config = (
            PPOConfig()
            .environment("CartPole-v1")
            .env_runners(num_envs_per_env_runner=1)
            # lr = 0.1 - [(0.1 - 0.000001) / 100000] * ts
            .training(lr_schedule=[[0, 0.1], [100000, 0.000001]])
        )
        for fw in framework_iterator(config, frameworks=("tf2", "tf")):
            algo = config.build()
            policy = algo.get_policy()
            for i in range(3):
                result = algo.train()
                print(
                    "{}={}".format(
                        NUM_AGENT_STEPS_TRAINED, result["info"][NUM_AGENT_STEPS_TRAINED]
                    )
                )
                print(
                    "{}={}".format(
                        NUM_AGENT_STEPS_SAMPLED, result["info"][NUM_AGENT_STEPS_SAMPLED]
                    )
                )
                global_timesteps = (
                    policy.global_timestep
                    if fw == "tf"
                    else policy.global_timestep.numpy()
                )
                print("global_timesteps={}".format(global_timesteps))
                expected_lr = 0.1 - ((0.1 - 0.000001) / 100000) * global_timesteps
                lr = policy.cur_lr
                if fw == "tf":
                    lr = policy.get_session().run(lr)
                check(lr, expected_lr, rtol=0.05)
            algo.stop()

    def test_query_evaluators(self):
        register_env("test", lambda _: gym.make("CartPole-v1"))
        config = (
            PPOConfig()
            .environment("test")
            .env_runners(
                num_env_runners=2,
                num_envs_per_env_runner=2,
                create_env_on_local_worker=True,
            )
            .training(train_batch_size=20, sgd_minibatch_size=5, num_sgd_iter=1)
        )
        for _ in framework_iterator(config, frameworks=("torch", "tf")):
            algo = config.build()
            results = algo.workers.foreach_worker(
                lambda w: w.total_rollout_fragment_length
            )
            results2 = algo.workers.foreach_worker_with_id(
                lambda i, w: (i, w.total_rollout_fragment_length)
            )
            results3 = algo.workers.foreach_worker(
                lambda w: w.foreach_env(lambda env: 1)
            )
            self.assertEqual(results, [10, 10, 10])
            self.assertEqual(results2, [(0, 10), (1, 10), (2, 10)])
            self.assertEqual(results3, [[1, 1], [1, 1], [1, 1]])
            algo.stop()

    def test_action_clipping(self):
        action_space = gym.spaces.Box(-2.0, 1.0, (3,))

        # Clipping: True (clip between Policy's action_space.low/high).
        ev = RolloutWorker(
            env_creator=lambda _: RandomEnv(
                config=dict(
                    action_space=action_space,
                    max_episode_len=10,
                    p_terminated=0.0,
                    check_action_bounds=True,
                )
            ),
            config=AlgorithmConfig()
            .multi_agent(
                policies={
                    "default_policy": PolicySpec(
                        policy_class=RandomPolicy,
                        config={"ignore_action_bounds": True},
                    )
                }
            )
            .env_runners(num_env_runners=0, batch_mode="complete_episodes")
            .environment(
                action_space=action_space, normalize_actions=False, clip_actions=True
            ),
        )
        sample = convert_ma_batch_to_sample_batch(ev.sample())
        # Check, whether the action bounds have been breached (expected).
        # We still arrived here b/c we clipped according to the Env's action
        # space.
        self.assertGreater(np.max(sample["actions"]), action_space.high[0])
        self.assertLess(np.min(sample["actions"]), action_space.low[0])
        ev.stop()

        # Clipping: False and RandomPolicy produces invalid actions.
        # Expect Env to complain.
        ev2 = RolloutWorker(
            env_creator=lambda _: RandomEnv(
                config=dict(
                    action_space=action_space,
                    max_episode_len=10,
                    p_terminated=0.0,
                    check_action_bounds=True,
                )
            ),
            # No normalization (+clipping) and no clipping ->
            # Should lead to Env complaining.
            config=AlgorithmConfig()
            .environment(
                normalize_actions=False,
                clip_actions=False,
                action_space=action_space,
            )
            .env_runners(batch_mode="complete_episodes", num_env_runners=0)
            .multi_agent(
                policies={
                    "default_policy": PolicySpec(
                        policy_class=RandomPolicy,
                        config={"ignore_action_bounds": True},
                    )
                }
            ),
        )
        self.assertRaisesRegex(ValueError, r"Illegal action", ev2.sample)
        ev2.stop()

        # Clipping: False and RandomPolicy produces valid (bounded) actions.
        # Expect "actions" in SampleBatch to be unclipped.
        ev3 = RolloutWorker(
            env_creator=lambda _: RandomEnv(
                config=dict(
                    action_space=action_space,
                    max_episode_len=10,
                    p_terminated=0.0,
                    check_action_bounds=True,
                )
            ),
            default_policy_class=RandomPolicy,
            config=AlgorithmConfig().env_runners(
                num_env_runners=0, batch_mode="complete_episodes"
            )
            # Should not be a problem as RandomPolicy abides to bounds.
            .environment(
                action_space=action_space, normalize_actions=False, clip_actions=False
            ),
        )
        sample = convert_ma_batch_to_sample_batch(ev3.sample())
        self.assertGreater(np.min(sample["actions"]), action_space.low[0])
        self.assertLess(np.max(sample["actions"]), action_space.high[0])
        ev3.stop()

    def test_action_normalization(self):
        action_space = gym.spaces.Box(0.0001, 0.0002, (5,))

        # Normalize: True (unsquash between Policy's action_space.low/high).
        ev = RolloutWorker(
            env_creator=lambda _: RandomEnv(
                config=dict(
                    action_space=action_space,
                    max_episode_len=10,
                    p_terminated=0.0,
                    check_action_bounds=True,
                )
            ),
            config=AlgorithmConfig()
            .multi_agent(
                policies={
                    "default_policy": PolicySpec(
                        policy_class=RandomPolicy,
                        config={"ignore_action_bounds": True},
                    )
                }
            )
            .env_runners(num_env_runners=0, batch_mode="complete_episodes")
            .environment(
                action_space=action_space, normalize_actions=True, clip_actions=False
            ),
        )
        sample = convert_ma_batch_to_sample_batch(ev.sample())
        # Check, whether the action bounds have been breached (expected).
        # We still arrived here b/c we unsquashed according to the Env's action
        # space.
        self.assertGreater(np.max(sample["actions"]), action_space.high[0])
        self.assertLess(np.min(sample["actions"]), action_space.low[0])
        ev.stop()

    def test_action_normalization_offline_dataset(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # create environment
            env = gym.make("Pendulum-v1")

            # create temp data with actions at min and max
            data = {
                "type": "SampleBatch",
                "actions": [[2.0], [-2.0]],
                "terminateds": [0.0, 0.0],
                "truncateds": [0.0, 0.0],
                "rewards": [0.0, 0.0],
                "obs": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
                "new_obs": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
            }

            data_file = os.path.join(tmp_dir, "data.json")

            with open(data_file, "w") as f:
                json.dump(data, f)

            # create input reader functions
            def dataset_reader_creator(ioctx):
                config = AlgorithmConfig().offline_data(
                    input_="dataset",
                    input_config={"format": "json", "paths": data_file},
                )
                _, shards = get_dataset_and_shards(config, num_workers=0)
                return DatasetReader(shards[0], ioctx)

            def json_reader_creator(ioctx):
                return JsonReader(data_file, ioctx)

            input_creators = [dataset_reader_creator, json_reader_creator]

            # actions_in_input_normalized, normalize_actions
            parameters = [
                (True, True),
                (True, False),
                (False, True),
                (False, False),
            ]

            # check that samples from dataset will be normalized if and only if
            # actions_in_input_normalized == False and
            # normalize_actions == True
            for input_creator in input_creators:
                for actions_in_input_normalized, normalize_actions in parameters:
                    ev = RolloutWorker(
                        env_creator=lambda _: env,
                        default_policy_class=MockPolicy,
                        config=AlgorithmConfig()
                        .env_runners(
                            num_env_runners=0,
                            rollout_fragment_length=1,
                        )
                        .environment(
                            normalize_actions=normalize_actions,
                            clip_actions=False,
                        )
                        .training(train_batch_size=1)
                        .offline_data(
                            offline_sampling=True,
                            actions_in_input_normalized=actions_in_input_normalized,
                            input_=input_creator,
                        ),
                    )

                    sample = ev.sample()

                    if normalize_actions and not actions_in_input_normalized:
                        # check if the samples from dataset are normalized properly
                        self.assertLessEqual(np.max(sample["actions"]), 1.0)
                        self.assertGreaterEqual(np.min(sample["actions"]), -1.0)
                    else:
                        # check if the samples from dataset are not normalized
                        self.assertGreater(np.max(sample["actions"]), 1.5)
                        self.assertLess(np.min(sample["actions"]), -1.5)

                    ev.stop()

    def test_action_immutability(self):
        action_space = gym.spaces.Box(0.0001, 0.0002, (5,))

        class ActionMutationEnv(RandomEnv):
            def init(self, config):
                self.test_case = config["test_case"]
                super().__init__(config=config)

            def step(self, action):
                # Check, whether the action is immutable.
                if action.flags.writeable:
                    self.test_case.assertFalse(
                        action.flags.writeable, "Action is mutable"
                    )
                return super().step(action)

        ev = RolloutWorker(
            env_creator=lambda _: ActionMutationEnv(
                config=dict(
                    test_case=self,
                    action_space=action_space,
                    max_episode_len=10,
                    p_terminated=0.0,
                    check_action_bounds=True,
                )
            ),
            config=AlgorithmConfig()
            .multi_agent(
                policies={
                    "default_policy": PolicySpec(
                        policy_class=RandomPolicy,
                        config={"ignore_action_bounds": True},
                    )
                }
            )
            .environment(action_space=action_space, clip_actions=False)
            .env_runners(batch_mode="complete_episodes", num_env_runners=0),
        )
        ev.sample()
        ev.stop()

    def test_reward_clipping(self):
        # Clipping: True (clip between -1.0 and 1.0).
        config = (
            AlgorithmConfig()
            .env_runners(num_env_runners=0, batch_mode="complete_episodes")
            .environment(clip_rewards=True)
        )
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv2(episode_length=10),
            default_policy_class=MockPolicy,
            config=config,
        )
        sample = convert_ma_batch_to_sample_batch(ev.sample())
        ws = WorkerSet._from_existing(
            local_worker=ev,
            remote_workers=[],
        )
        self.assertEqual(max(sample["rewards"]), 1)
        result = collect_metrics(ws, [])
        # Shows different behavior when connector is on/off.
        if config.enable_connectors:
            # episode_reward_mean shows the correct clipped value.
            self.assertEqual(result["episode_reward_mean"], 10)
        else:
            # episode_reward_mean shows the unclipped raw value
            # when connector is off, and old env_runner v1 is used.
            self.assertEqual(result["episode_reward_mean"], 1000)
        ev.stop()

        # Clipping in certain range (-2.0, 2.0).
        ev2 = RolloutWorker(
            env_creator=lambda _: RandomEnv(
                dict(
                    reward_space=gym.spaces.Box(low=-10, high=10, shape=()),
                    p_terminated=0.0,
                    max_episode_len=10,
                )
            ),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .env_runners(num_env_runners=0, batch_mode="complete_episodes")
            .environment(clip_rewards=2.0),
        )
        sample = convert_ma_batch_to_sample_batch(ev2.sample())
        self.assertEqual(max(sample["rewards"]), 2.0)
        self.assertEqual(min(sample["rewards"]), -2.0)
        self.assertLess(np.mean(sample["rewards"]), 0.5)
        self.assertGreater(np.mean(sample["rewards"]), -0.5)
        ev2.stop()

        # Clipping: Off.
        ev2 = RolloutWorker(
            env_creator=lambda _: MockEnv2(episode_length=10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .env_runners(num_env_runners=0, batch_mode="complete_episodes")
            .environment(clip_rewards=False),
        )
        sample = convert_ma_batch_to_sample_batch(ev2.sample())
        ws2 = WorkerSet._from_existing(
            local_worker=ev2,
            remote_workers=[],
        )
        self.assertEqual(max(sample["rewards"]), 100)
        result2 = collect_metrics(ws2, [])
        self.assertEqual(result2["episode_reward_mean"], 1000)
        ev2.stop()

    def test_metrics(self):
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv(episode_length=10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=100,
                num_env_runners=0,
                batch_mode="complete_episodes",
            ),
        )
        remote_ev = ray.remote(RolloutWorker).remote(
            env_creator=lambda _: MockEnv(episode_length=10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=100,
                num_env_runners=0,
                batch_mode="complete_episodes",
            ),
        )
        ws = WorkerSet._from_existing(
            local_worker=ev,
            remote_workers=[remote_ev],
        )
        ev.sample()
        ray.get(remote_ev.sample.remote())
        result = collect_metrics(ws)
        self.assertEqual(result["episodes_this_iter"], 20)
        self.assertEqual(result["episode_reward_mean"], 10)
        ev.stop()

    def test_auto_vectorization(self):
        ev = RolloutWorker(
            env_creator=lambda cfg: MockEnv(episode_length=20, config=cfg),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=2,
                num_envs_per_env_runner=8,
                num_env_runners=0,
                batch_mode="truncate_episodes",
            ),
        )
        ws = WorkerSet._from_existing(
            local_worker=ev,
            remote_workers=[],
        )
        for _ in range(8):
            batch = ev.sample()
            self.assertEqual(batch.count, 16)
        result = collect_metrics(ws, [])
        self.assertEqual(result["episodes_this_iter"], 0)
        for _ in range(8):
            batch = ev.sample()
            self.assertEqual(batch.count, 16)
        result = collect_metrics(ws, [])
        self.assertEqual(result["episodes_this_iter"], 8)
        indices = []
        for env in ev.async_env.vector_env.envs:
            self.assertEqual(env.unwrapped.config.worker_index, 0)
            indices.append(env.unwrapped.config.vector_index)
        self.assertEqual(indices, [0, 1, 2, 3, 4, 5, 6, 7])
        ev.stop()

    def test_batches_larger_when_vectorized(self):
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv(episode_length=8),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=4,
                num_envs_per_env_runner=4,
                num_env_runners=0,
                batch_mode="truncate_episodes",
            ),
        )
        ws = WorkerSet._from_existing(
            local_worker=ev,
            remote_workers=[],
        )
        batch = ev.sample()
        self.assertEqual(batch.count, 16)
        result = collect_metrics(ws, [])
        self.assertEqual(result["episodes_this_iter"], 0)
        batch = ev.sample()
        result = collect_metrics(ws, [])
        self.assertEqual(result["episodes_this_iter"], 4)
        ev.stop()

    def test_vector_env_support(self):
        # Test a vector env that contains 8 actual envs
        # (MockEnv instances).
        ev = RolloutWorker(
            env_creator=(lambda _: VectorizedMockEnv(episode_length=20, num_envs=8)),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=10,
                num_env_runners=0,
                batch_mode="truncate_episodes",
            ),
        )
        ws = WorkerSet._from_existing(
            local_worker=ev,
            remote_workers=[],
        )
        for _ in range(8):
            batch = ev.sample()
            self.assertEqual(batch.count, 10)
        result = collect_metrics(ws, [])
        self.assertEqual(result["episodes_this_iter"], 0)
        for _ in range(8):
            batch = ev.sample()
            self.assertEqual(batch.count, 10)
        result = collect_metrics(ws, [])
        self.assertEqual(result["episodes_this_iter"], 8)
        ev.stop()

        # Test a vector env that pretends(!) to contain 4 envs, but actually
        # only has 1 (CartPole).
        ev = RolloutWorker(
            env_creator=(lambda _: MockVectorEnv(20, mocked_num_envs=4)),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=10,
                num_env_runners=0,
                batch_mode="truncate_episodes",
            ),
        )
        ws = WorkerSet._from_existing(
            local_worker=ev,
            remote_workers=[],
        )
        for _ in range(8):
            batch = ev.sample()
            self.assertEqual(batch.count, 10)
        result = collect_metrics(ws, [])
        self.assertGreater(result["episodes_this_iter"], 3)
        for _ in range(8):
            batch = ev.sample()
            self.assertEqual(batch.count, 10)
        result = collect_metrics(ws, [])
        self.assertGreater(result["episodes_this_iter"], 6)
        ev.stop()

    def test_truncate_episodes(self):
        ev_env_steps = RolloutWorker(
            env_creator=lambda _: MockEnv(10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=15,
                num_env_runners=0,
                batch_mode="truncate_episodes",
            ),
        )
        batch = ev_env_steps.sample()
        self.assertEqual(batch.count, 15)
        self.assertTrue(issubclass(type(batch), (SampleBatch, MultiAgentBatch)))
        ev_env_steps.stop()

        action_space = Discrete(2)
        obs_space = Box(float("-inf"), float("inf"), (4,), dtype=np.float32)
        ev_agent_steps = RolloutWorker(
            env_creator=lambda _: MultiAgentCartPole({"num_agents": 4}),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .env_runners(
                num_env_runners=0,
                batch_mode="truncate_episodes",
                rollout_fragment_length=301,
            )
            .multi_agent(
                policies={"pol0", "pol1"},
                policy_mapping_fn=(
                    lambda agent_id, episode, worker, **kwargs: "pol0"
                    if agent_id == 0
                    else "pol1"
                ),
            )
            .environment(action_space=action_space, observation_space=obs_space),
        )
        batch = ev_agent_steps.sample()
        self.assertTrue(isinstance(batch, MultiAgentBatch))
        self.assertGreater(batch.agent_steps(), 301)
        self.assertEqual(batch.env_steps(), 301)
        ev_agent_steps.stop()

        ev_agent_steps = RolloutWorker(
            env_creator=lambda _: MultiAgentCartPole({"num_agents": 4}),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .env_runners(
                num_env_runners=0,
                rollout_fragment_length=301,
            )
            .multi_agent(
                count_steps_by="agent_steps",
                policies={"pol0", "pol1"},
                policy_mapping_fn=(
                    lambda agent_id, episode, worker, **kwargs: "pol0"
                    if agent_id == 0
                    else "pol1"
                ),
            ),
        )
        batch = ev_agent_steps.sample()
        self.assertTrue(isinstance(batch, MultiAgentBatch))
        self.assertLess(batch.env_steps(), 301)
        # When counting agent steps, the count may be slightly larger than
        # rollout_fragment_length, b/c we have up to N agents stepping in each
        # env step and we only check, whether we should build after each env
        # step.
        self.assertGreaterEqual(batch.agent_steps(), 301)
        ev_agent_steps.stop()

    def test_complete_episodes(self):
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv(10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=5,
                num_env_runners=0,
                batch_mode="complete_episodes",
            ),
        )
        batch = ev.sample()
        self.assertEqual(batch.count, 10)
        ev.stop()

    def test_complete_episodes_packing(self):
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv(10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=15,
                num_env_runners=0,
                batch_mode="complete_episodes",
            ),
        )
        batch = ev.sample()
        batch = convert_ma_batch_to_sample_batch(batch)
        self.assertEqual(batch.count, 20)
        self.assertEqual(
            batch["t"].tolist(),
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        )
        ev.stop()

    def test_filter_sync(self):
        ev = RolloutWorker(
            env_creator=lambda _: gym.make("CartPole-v1"),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                num_env_runners=0,
                observation_filter="ConcurrentMeanStdFilter",
            ),
        )
        time.sleep(2)
        ev.sample()
        filters = ev.get_filters(flush_after=True)
        obs_f = filters[DEFAULT_POLICY_ID]
        self.assertNotEqual(obs_f.running_stats.n, 0)
        self.assertNotEqual(obs_f.buffer.n, 0)
        ev.stop()

    def test_get_filters(self):
        ev = RolloutWorker(
            env_creator=lambda _: gym.make("CartPole-v1"),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                observation_filter="ConcurrentMeanStdFilter",
                num_env_runners=0,
            ),
        )
        self.sample_and_flush(ev)
        filters = ev.get_filters(flush_after=False)
        time.sleep(2)
        filters2 = ev.get_filters(flush_after=False)
        obs_f = filters[DEFAULT_POLICY_ID]
        obs_f2 = filters2[DEFAULT_POLICY_ID]
        self.assertGreaterEqual(obs_f2.running_stats.n, obs_f.running_stats.n)
        self.assertGreaterEqual(obs_f2.buffer.n, obs_f.buffer.n)
        ev.stop()

    def test_sync_filter(self):
        ev = RolloutWorker(
            env_creator=lambda _: gym.make("CartPole-v1"),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                observation_filter="ConcurrentMeanStdFilter",
                num_env_runners=0,
            ),
        )
        obs_f = self.sample_and_flush(ev)

        # Current State
        filters = ev.get_filters(flush_after=False)
        obs_f = filters[DEFAULT_POLICY_ID]

        self.assertLessEqual(obs_f.buffer.n, 20)

        new_obsf = obs_f.copy()
        new_obsf.running_stats.num_pushes = 100
        ev.sync_filters({DEFAULT_POLICY_ID: new_obsf})
        filters = ev.get_filters(flush_after=False)
        obs_f = filters[DEFAULT_POLICY_ID]
        self.assertGreaterEqual(obs_f.running_stats.n, 100)
        self.assertLessEqual(obs_f.buffer.n, 20)
        ev.stop()

    def test_extra_python_envs(self):
        extra_envs = {"env_key_1": "env_value_1", "env_key_2": "env_value_2"}
        self.assertFalse("env_key_1" in os.environ)
        self.assertFalse("env_key_2" in os.environ)
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv(10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .python_environment(extra_python_environs_for_driver=extra_envs)
            .env_runners(num_env_runners=0),
        )
        self.assertTrue("env_key_1" in os.environ)
        self.assertTrue("env_key_2" in os.environ)
        ev.stop()

        # reset to original
        del os.environ["env_key_1"]
        del os.environ["env_key_2"]

    def test_no_env_seed(self):
        ev = RolloutWorker(
            env_creator=lambda _: MockVectorEnv(20, mocked_num_envs=8),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(num_env_runners=0).debugging(seed=1),
        )
        assert not hasattr(ev.env, "seed")
        ev.stop()

    def test_multi_env_seed(self):
        ev = RolloutWorker(
            env_creator=lambda _: MockEnv2(100),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .env_runners(num_envs_per_env_runner=3, num_env_runners=0)
            .debugging(seed=1),
        )
        # Make sure we can properly sample from the wrapped env.
        ev.sample()
        # Make sure all environments got a different deterministic seed.
        seeds = ev.foreach_env(lambda env: env.rng_seed)
        self.assertEqual(seeds, [1, 2, 3])
        ev.stop()

    def test_determine_spaces_for_multi_agent_dict(self):
        class MockMultiAgentEnv(MultiAgentEnv):
            """A mock testing MultiAgentEnv that doesn't call super.__init__()."""

            def __init__(self):
                # Intentinoally don't call super().__init__(),
                # so this env doesn't have
                # `self._[action|observation]_space_in_preferred_format`attributes.
                self.observation_space = gym.spaces.Discrete(2)
                self.action_space = gym.spaces.Discrete(2)

            def reset(self, *, seed=None, options=None):
                pass

            def step(self, action_dict):
                obs = {1: [0, 0], 2: [1, 1]}
                rewards = {1: 0, 2: 0}
                terminateds = truncated = {1: False, 2: False, "__all__": False}
                infos = {1: {}, 2: {}}
                return obs, rewards, terminateds, truncated, infos

        ev = RolloutWorker(
            env_creator=lambda _: MockMultiAgentEnv(),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig()
            .env_runners(num_envs_per_env_runner=3, num_env_runners=0)
            .multi_agent(policies={"policy_1", "policy_2"})
            .debugging(seed=1),
        )
        # The fact that this RolloutWorker can be created without throwing
        # exceptions means AlgorithmConfig.get_multi_agent_setup() is
        # handling multi-agent user environments properly.
        self.assertIsNotNone(ev)

    def test_wrap_multi_agent_env(self):
        from ray.rllib.env.tests.test_multi_agent_env import BasicMultiAgent

        ev = RolloutWorker(
            env_creator=lambda _: BasicMultiAgent(10),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=5,
                batch_mode="complete_episodes",
                num_env_runners=0,
            ),
        )
        # Make sure we can properly sample from the wrapped env.
        ev.sample()
        # Make sure the resulting environment is indeed still an
        self.assertTrue(isinstance(ev.env.unwrapped, MultiAgentEnv))
        self.assertTrue(isinstance(ev.env, gym.Env))
        ev.stop()

    def test_no_training(self):
        class NoTrainingEnv(MockEnv):
            def __init__(self, episode_length, training_enabled):
                super().__init__(episode_length)
                self.training_enabled = training_enabled

            def step(self, action):
                obs, rew, terminated, truncated, info = super().step(action)
                return (
                    obs,
                    rew,
                    terminated,
                    truncated,
                    {**info, "training_enabled": self.training_enabled},
                )

        ev = RolloutWorker(
            env_creator=lambda _: NoTrainingEnv(10, True),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=5,
                batch_mode="complete_episodes",
                num_env_runners=0,
            ),
        )
        batch = ev.sample()
        batch = convert_ma_batch_to_sample_batch(batch)
        self.assertEqual(batch.count, 10)
        self.assertEqual(len(batch["obs"]), 10)
        ev.stop()

        ev = RolloutWorker(
            env_creator=lambda _: NoTrainingEnv(10, False),
            default_policy_class=MockPolicy,
            config=AlgorithmConfig().env_runners(
                rollout_fragment_length=5,
                batch_mode="complete_episodes",
                num_env_runners=0,
            ),
        )
        batch = ev.sample()
        self.assertTrue(isinstance(batch, MultiAgentBatch))
        self.assertEqual(len(batch.policy_batches), 0)
        ev.stop()

    def sample_and_flush(self, ev):
        time.sleep(2)
        ev.sample()
        filters = ev.get_filters(flush_after=True)
        obs_f = filters[DEFAULT_POLICY_ID]
        self.assertNotEqual(obs_f.running_stats.n, 0)
        self.assertNotEqual(obs_f.buffer.n, 0)
        return obs_f


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
