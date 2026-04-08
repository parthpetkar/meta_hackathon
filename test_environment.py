"""Tests for the Meta Hackathon CI/CD repair environment.

Validates reset/step/state API behavior, grader output ranges, and
full episode completion across all four tasks (easy/medium/security/hard).
"""

from __future__ import annotations

import os

import pytest

# Disable rubric during tests to avoid external API calls.
os.environ["META_HACKATHON_RUBRIC_ENABLED"] = "false"

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import MetaHackathonAction, MetaHackathonObservation
from server.meta_hackathon_environment import MetaHackathonCICDRepairEnvironment
from server.graders import grade_episode, step_reward
from server.scenarios import list_task_keys, get_scenario


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=list_task_keys())
def env_for_task(request):
    """Yield a fresh environment instance for each task key."""
    env = MetaHackathonCICDRepairEnvironment(task_key=request.param)
    return env, request.param


@pytest.fixture
def easy_env():
    return MetaHackathonCICDRepairEnvironment(task_key="easy")


@pytest.fixture
def medium_env():
    return MetaHackathonCICDRepairEnvironment(task_key="medium")


@pytest.fixture
def security_env():
    return MetaHackathonCICDRepairEnvironment(task_key="security")


@pytest.fixture
def hard_env():
    return MetaHackathonCICDRepairEnvironment(task_key="hard")


# ---------------------------------------------------------------------------
# reset() tests
# ---------------------------------------------------------------------------


class TestReset:
    """Verify reset() returns valid initial observations."""

    def test_reset_returns_observation(self, env_for_task):
        env, task_key = env_for_task
        obs = env.reset()
        assert isinstance(obs, MetaHackathonObservation)

    def test_reset_not_done(self, env_for_task):
        env, _ = env_for_task
        obs = env.reset()
        assert obs.done is False

    def test_reset_zero_reward(self, env_for_task):
        env, _ = env_for_task
        obs = env.reset()
        assert obs.reward == 0.0

    def test_reset_has_task_id(self, env_for_task):
        env, _ = env_for_task
        obs = env.reset()
        assert obs.task_id, "task_id must be non-empty after reset"

    def test_reset_has_difficulty(self, env_for_task):
        env, _ = env_for_task
        obs = env.reset()
        assert obs.difficulty in {"easy", "medium", "security", "hard"}

    def test_reset_pipeline_stages(self, env_for_task):
        env, _ = env_for_task
        obs = env.reset()
        assert isinstance(obs.pipeline_stages, dict)
        assert set(obs.pipeline_stages.keys()) == {"build", "test", "deploy"}

    def test_reset_clears_history(self, env_for_task):
        env, _ = env_for_task
        env.reset()
        # Step once, then reset again and verify history is empty.
        env.step(MetaHackathonAction(operation="view_logs"))
        obs = env.reset()
        assert obs.action_history == []

    def test_reset_has_metadata(self, env_for_task):
        env, _ = env_for_task
        obs = env.reset()
        assert isinstance(obs.metadata, dict)
        assert "task_key" in obs.metadata
        assert "max_steps" in obs.metadata


# ---------------------------------------------------------------------------
# step() tests
# ---------------------------------------------------------------------------


class TestStep:
    """Verify step() returns correct types and handles various actions."""

    def test_step_returns_observation(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(MetaHackathonAction(operation="view_logs"))
        assert isinstance(obs, MetaHackathonObservation)

    def test_step_has_reward(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(MetaHackathonAction(operation="view_logs"))
        assert isinstance(obs.reward, (int, float))

    def test_step_invalid_operation_negative_reward(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(MetaHackathonAction(operation="invalid_op"))
        assert obs.reward < 0

    def test_step_view_logs_relevant(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(MetaHackathonAction(operation="view_logs", target="build"))
        assert obs.reward == pytest.approx(0.12)

    def test_step_inspect_config(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(MetaHackathonAction(operation="inspect_config", target="build"))
        assert obs.reward == pytest.approx(0.12)

    def test_step_set_hypothesis(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(
            MetaHackathonAction(
                operation="set_hypothesis",
                value="merge conflict markers are blocking build validation",
            )
        )
        # Correct hypothesis should give positive reward.
        assert obs.reward > 0

    def test_step_count_increments(self, easy_env):
        easy_env.reset()
        easy_env.step(MetaHackathonAction(operation="view_logs"))
        state = easy_env.state
        assert state.step_count == 1

    def test_redundant_action_penalty(self, easy_env):
        easy_env.reset()
        easy_env.step(MetaHackathonAction(operation="view_logs", target="build"))
        obs2 = easy_env.step(MetaHackathonAction(operation="view_logs", target="build"))
        assert obs2.redundant_actions >= 1

    def test_malformed_add_dependency_returns_structured_error(self, easy_env):
        easy_env.reset()
        obs = easy_env.step(MetaHackathonAction(operation="add_dependency", target="build", value=""))
        
        # It should return a structured error
        assert "Malformed action: add_dependency requires a 'value' string" in obs.metadata.get("error", "")
        # Reward should be partial penalty (-0.05) instead of full penalty (-0.18)
        assert -0.06 <= obs.reward <= -0.04


# ---------------------------------------------------------------------------
# state() tests
# ---------------------------------------------------------------------------


class TestState:
    """Verify state() returns expected structure."""

    def test_state_has_episode_id(self, easy_env):
        easy_env.reset()
        state = easy_env.state
        assert hasattr(state, "episode_id")
        assert isinstance(state.episode_id, str)
        assert len(state.episode_id) > 0

    def test_state_has_step_count(self, easy_env):
        easy_env.reset()
        state = easy_env.state
        assert hasattr(state, "step_count")
        assert state.step_count == 0


# ---------------------------------------------------------------------------
# Full episode tests (deterministic fallback trajectories)
# ---------------------------------------------------------------------------


FALLBACK_TRAJECTORIES = {
    "easy": [
        ("view_logs", "build", ""),
        ("inspect_config", "build", ""),
        ("set_hypothesis", "", "merge conflict markers are blocking build validation"),
        ("modify_config", "build", "sync branch and resolve merge conflict"),
        ("rerun_pipeline", "", ""),
        ("verify_fix", "", ""),
        ("finalize", "", ""),
    ],
    "medium": [
        ("view_logs", "build", ""),
        ("inspect_config", "build", ""),
        ("inspect_dockerfile", "build", ""),
        ("set_hypothesis", "", "requests and urllib3 are incompatible"),
        ("add_dependency", "build", "pin compatible requests urllib3 versions"),
        ("rerun_pipeline", "", ""),
        ("set_hypothesis", "", "docker install order mismatch still causing flaky build"),
        ("modify_config", "build", "reorder docker install steps"),
        ("rerun_pipeline", "", ""),
        ("verify_fix", "", ""),
        ("finalize", "", ""),
    ],
    "security": [
        ("view_logs", "deploy", ""),
        ("inspect_permissions", "deploy", ""),
        ("set_hypothesis", "", "artifact registry push fails because deployer lacks writer permissions"),
        ("modify_config", "deploy", "grant artifactregistry writer to ci-deployer"),
        ("rerun_pipeline", "", ""),
        ("view_logs", "deploy", ""),
        ("inspect_dockerfile", "build", ""),
        ("set_hypothesis", "", "Dockerfile exposes API_KEY and must use secret manager reference"),
        ("modify_config", "deploy", "replace Dockerfile API_KEY with secret manager reference"),
        ("rerun_pipeline", "", ""),
        ("verify_fix", "", ""),
        ("finalize", "", ""),
    ],
    "hard": [
        ("inspect_permissions", "build", ""),
        ("set_hypothesis", "", "service-a publish is failing because artifactregistry writer permission is missing"),
        ("modify_config", "build", "grant artifactregistry writer to service-a publisher"),
        ("rerun_pipeline", "", ""),
        ("inspect_config", "deploy", ""),
        ("set_hypothesis", "", "service-b should rollback to the last stable image revision"),
        ("modify_config", "deploy", "rollback service-b to stable image revision"),
        ("rerun_pipeline", "", ""),
        ("view_logs", "deploy", ""),
        ("set_hypothesis", "", "service-b rollout timeout should be increased to 20m after rollback"),
        ("modify_config", "deploy", "increase rollout timeout to 20m"),
        ("rerun_pipeline", "", ""),
        ("verify_fix", "", ""),
        ("finalize", "", ""),
    ],
}


class TestFullEpisode:
    """Run deterministic fallback trajectories and validate scoring."""

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_episode_completes(self, task_key):
        env = MetaHackathonCICDRepairEnvironment(task_key=task_key)
        obs = env.reset()
        trajectory = FALLBACK_TRAJECTORIES[task_key]
        rewards = []

        for operation, target, value in trajectory:
            obs = env.step(MetaHackathonAction(operation=operation, target=target, value=value))
            rewards.append(obs.reward)

        assert obs.done is True, f"{task_key}: episode should be done after finalize"

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_episode_resolves(self, task_key):
        env = MetaHackathonCICDRepairEnvironment(task_key=task_key)
        env.reset()
        trajectory = FALLBACK_TRAJECTORIES[task_key]

        obs = None
        for operation, target, value in trajectory:
            obs = env.step(MetaHackathonAction(operation=operation, target=target, value=value))

        assert obs.incident_resolved is True, f"{task_key}: incident should be resolved"

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_final_score_in_range(self, task_key):
        env = MetaHackathonCICDRepairEnvironment(task_key=task_key)
        env.reset()
        trajectory = FALLBACK_TRAJECTORIES[task_key]

        obs = None
        for operation, target, value in trajectory:
            obs = env.step(MetaHackathonAction(operation=operation, target=target, value=value))

        assert 0.0 <= obs.final_score <= 1.0, (
            f"{task_key}: final_score={obs.final_score} out of [0,1] range"
        )

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_all_rewards_bounded(self, task_key):
        env = MetaHackathonCICDRepairEnvironment(task_key=task_key)
        env.reset()
        trajectory = FALLBACK_TRAJECTORIES[task_key]
        rewards = []

        for operation, target, value in trajectory:
            obs = env.step(MetaHackathonAction(operation=operation, target=target, value=value))
            rewards.append(obs.reward)

        for i, r in enumerate(rewards):
            assert -1.0 <= r <= 1.0, (
                f"{task_key} step {i}: reward={r} out of [-1,1] range"
            )

    def test_difficulty_gradient(self):
        """Verify that easy > medium > hard scores (deterministic baseline)."""
        scores = {}
        for task_key in list_task_keys():
            env = MetaHackathonCICDRepairEnvironment(task_key=task_key)
            env.reset()
            trajectory = FALLBACK_TRAJECTORIES[task_key]

            obs = None
            for operation, target, value in trajectory:
                obs = env.step(MetaHackathonAction(operation=operation, target=target, value=value))

            scores[task_key] = obs.final_score

        assert scores["easy"] > scores["medium"], (
            f"easy ({scores['easy']}) should score higher than medium ({scores['medium']})"
        )
        assert scores["medium"] > scores["hard"], (
            f"medium ({scores['medium']}) should score higher than hard ({scores['hard']})"
        )


# ---------------------------------------------------------------------------
# Grader unit tests
# ---------------------------------------------------------------------------


class TestGrader:
    """Verify grade_episode and step_reward produce valid outputs."""

    def test_grade_episode_returns_float(self):
        score = grade_episode(
            difficulty="easy",
            issue_count=1,
            solved_issues=1,
            required_inspection_actions={"view_logs", "inspect_config"},
            used_inspection_actions={"view_logs", "inspect_config"},
            hypothesis_hits=1,
            family_hits=0,
            fix_hits=1,
            final_resolved=True,
            action_count=7,
            max_steps=8,
            redundant_actions=0,
            destructive_actions=0,
            pipeline_health=1.0,
            wrong_fixes=0,
        )
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_grade_episode_unresolved_lower(self):
        resolved = grade_episode(
            difficulty="easy",
            issue_count=1,
            solved_issues=1,
            required_inspection_actions={"view_logs"},
            used_inspection_actions={"view_logs"},
            hypothesis_hits=1,
            family_hits=0,
            fix_hits=1,
            final_resolved=True,
            action_count=7,
            max_steps=8,
            redundant_actions=0,
            destructive_actions=0,
            pipeline_health=1.0,
            wrong_fixes=0,
        )
        unresolved = grade_episode(
            difficulty="easy",
            issue_count=1,
            solved_issues=0,
            required_inspection_actions={"view_logs"},
            used_inspection_actions={"view_logs"},
            hypothesis_hits=0,
            family_hits=0,
            fix_hits=0,
            final_resolved=False,
            action_count=7,
            max_steps=8,
            redundant_actions=0,
            destructive_actions=0,
            pipeline_health=1.0,
            wrong_fixes=0,
        )
        assert resolved > unresolved

    def test_step_reward_correct_hypothesis(self):
        r = step_reward(
            operation="set_hypothesis",
            was_redundant=False,
            inspection_relevant=False,
            hypothesis_correct_first_try=True,
            hypothesis_correct_retry=False,
            fix_correct_for_issue=False,
            fix_partial_for_issue=False,
            fix_wrong_for_issue=False,
            is_destructive_fix=False,
            red_herring_fix=False,
            rerun_after_valid_fix=False,
            verify_success=False,
            verify_failed=False,
            finalize_correct=False,
            finalize_partial=False,
            finalize_incorrect=False,
        )
        assert r == pytest.approx(0.22)

    def test_step_reward_wrong_hypothesis(self):
        r = step_reward(
            operation="set_hypothesis",
            was_redundant=False,
            inspection_relevant=False,
            hypothesis_correct_first_try=False,
            hypothesis_correct_retry=False,
            fix_correct_for_issue=False,
            fix_partial_for_issue=False,
            fix_wrong_for_issue=False,
            is_destructive_fix=False,
            red_herring_fix=False,
            rerun_after_valid_fix=False,
            verify_success=False,
            verify_failed=False,
            finalize_correct=False,
            finalize_partial=False,
            finalize_incorrect=False,
        )
        assert r == pytest.approx(-0.10)

    def test_step_reward_relevant_inspection(self):
        r = step_reward(
            operation="view_logs",
            was_redundant=False,
            inspection_relevant=True,
            hypothesis_correct_first_try=False,
            hypothesis_correct_retry=False,
            fix_correct_for_issue=False,
            fix_partial_for_issue=False,
            fix_wrong_for_issue=False,
            is_destructive_fix=False,
            red_herring_fix=False,
            rerun_after_valid_fix=False,
            verify_success=False,
            verify_failed=False,
            finalize_correct=False,
            finalize_partial=False,
            finalize_incorrect=False,
        )
        assert r == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# Scenario tests
# ---------------------------------------------------------------------------


class TestScenarios:
    """Verify scenario definitions are consistent."""

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_scenario_has_incident_chain(self, task_key):
        scenario = get_scenario(task_key)
        assert len(scenario.incident_chain) >= 1

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_scenario_max_steps_positive(self, task_key):
        scenario = get_scenario(task_key)
        assert scenario.max_steps > 0

    @pytest.mark.parametrize("task_key", list_task_keys())
    def test_scenario_has_variants(self, task_key):
        scenario = get_scenario(task_key)
        assert len(scenario.variants) >= 1

    def test_list_task_keys_has_four(self):
        keys = list_task_keys()
        assert len(keys) == 4
        assert set(keys) == {"easy", "medium", "security", "hard"}


# ---------------------------------------------------------------------------
# Rubric Judge Caching tests
# ---------------------------------------------------------------------------


class TestRubricJudgeCaching:
    """Verify rubric judge efficiently caches hypotheses."""

    def test_rubric_judge_caches_hypothesis(self):
        from server.rubric_judge import OpenEnvLLMJudgeAdapter, _JUDGE_CACHE
        # Clear cache for isolated test
        _JUDGE_CACHE.clear()

        judge = OpenEnvLLMJudgeAdapter(enabled=False, model_name="test", timeout_seconds=1)
        payload = {
            "task_id": "caching_test",
            "evidence": {"hypothesis_history": ["this is a test hypothesis"]}
        }

        # First call should miss cache and use heuristic fallback (since enabled=False)
        result1 = judge.evaluate_hypothesis_quality(payload)
        
        # Manually alter the cached result string to prove the second call reads from cache
        cache_key = judge._prompt_cache_key(payload)
        assert cache_key in _JUDGE_CACHE
        _JUDGE_CACHE[cache_key].rationale = "cached_rationale_string"

        # Second call should hit the cache instead of computing fallback again
        result2 = judge.evaluate_hypothesis_quality(payload)
        assert result2.rationale == "cached_rationale_string"
        assert result2.score == result1.score

