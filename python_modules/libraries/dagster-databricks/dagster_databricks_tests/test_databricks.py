from unittest import mock

import dagster
import dagster_databricks
import dagster_pyspark
import pytest
from dagster import build_op_context
from dagster_databricks.databricks import DatabricksClient, DatabricksError, DatabricksJobRunner
from dagster_databricks.types import (
    DatabricksRunLifeCycleState,
    DatabricksRunResultState,
)
from databricks.sdk.service import compute, jobs
from pytest_mock import MockerFixture

HOST = "https://uksouth.azuredatabricks.net"
TOKEN = "super-secret-token"


@mock.patch("databricks.sdk.JobsAPI.submit")
def test_databricks_submit_job_existing_cluster(mock_submit_run, databricks_run_config):
    mock_submit_run_response = mock.Mock()
    mock_submit_run_response.bind.return_value = {"run_id": 1}
    mock_submit_run.return_value = mock_submit_run_response

    runner = DatabricksJobRunner(HOST, TOKEN)
    task = databricks_run_config.pop("task")
    expected_task = jobs.SubmitTask.from_dict(task)
    expected_task.existing_cluster_id = databricks_run_config["cluster"]["existing"]
    expected_task.task_key = "dagster-task"
    expected_task.libraries = [
        compute.Library(pypi=compute.PythonPyPiLibrary(package=f"dagster=={dagster.__version__}")),
        compute.Library(
            pypi=compute.PythonPyPiLibrary(
                package=f"dagster-databricks=={dagster_databricks.__version__}"
            )
        ),
        compute.Library(
            pypi=compute.PythonPyPiLibrary(
                package=f"dagster-pyspark=={dagster_pyspark.__version__}"
            )
        ),
    ]
    expected_health = [
        jobs.JobsHealthRule.from_dict(h) for h in databricks_run_config["job_health_settings"]
    ]

    runner.submit_run(databricks_run_config, task)
    mock_submit_run.assert_called_with(
        run_name=databricks_run_config["run_name"],
        tasks=[expected_task],
        health=expected_health,
        idempotency_token=databricks_run_config["idempotency_token"],
        timeout_seconds=databricks_run_config["timeout_seconds"],
    )

    databricks_run_config["install_default_libraries"] = False
    expected_task.libraries = None

    runner.submit_run(databricks_run_config, task)
    mock_submit_run.assert_called_with(
        run_name=databricks_run_config["run_name"],
        tasks=[expected_task],
        health=expected_health,
        idempotency_token=databricks_run_config["idempotency_token"],
        timeout_seconds=databricks_run_config["timeout_seconds"],
    )


@mock.patch("databricks.sdk.JobsAPI.submit")
def test_databricks_submit_job_new_cluster(mock_submit_run, databricks_run_config):
    mock_submit_run_response = mock.Mock()
    mock_submit_run_response.bind.return_value = {"run_id": 1}
    mock_submit_run.return_value = mock_submit_run_response

    runner = DatabricksJobRunner(HOST, TOKEN)

    NEW_CLUSTER = {
        "size": {"num_workers": 1},
        "spark_version": "6.5.x-scala2.11",
        "nodes": {"node_types": {"node_type_id": "Standard_DS3_v2"}},
    }
    databricks_run_config["cluster"] = {"new": NEW_CLUSTER}

    databricks_run_config.update(
        {
            "email_notifications": {
                "on_duration_warning_threshold_exceeded": ["user@alerts.com"],
                "on_failure": ["user@alerts.com"],
                "on_start": ["user@alerts.com"],
                "on_success": ["user@alerts.com"],
                "no_alert_for_skipped_runs": True,
            },
            "notification_settings": {
                "no_alert_for_canceled_runs": True,
                "no_alert_for_skipped_runs": True,
            },
            "webhook_notifications": {
                "on_duration_warning_threshold_exceeded": [{"id": "abc123"}],
                "on_failure": [{"id": "abc123"}],
                "on_start": [{"id": "abc123"}],
                "on_success": [{"id": "abc123"}],
            },
            "job_health_settings": [
                {
                    "metric": "RUN_DURATION_SECONDS",
                    "op": "GREATER_THAN",
                    "value": 100,
                }
            ],
            "idempotency_token": "abc123",
            "timeout_seconds": 100,
        },
    )

    task = databricks_run_config.pop("task")
    expected_task = jobs.SubmitTask.from_dict(task)
    expected_task.task_key = "dagster-task"
    expected_task.new_cluster = compute.ClusterSpec(
        node_type_id=NEW_CLUSTER["nodes"]["node_types"]["node_type_id"],
        spark_version=NEW_CLUSTER["spark_version"],
        num_workers=NEW_CLUSTER["size"]["num_workers"],
        custom_tags={"__dagster_version": dagster.__version__},
    )
    expected_task.libraries = [
        compute.Library(pypi=compute.PythonPyPiLibrary(package=f"dagster=={dagster.__version__}")),
        compute.Library(
            pypi=compute.PythonPyPiLibrary(
                package=f"dagster-databricks=={dagster_databricks.__version__}"
            )
        ),
        compute.Library(
            pypi=compute.PythonPyPiLibrary(
                package=f"dagster-pyspark=={dagster_pyspark.__version__}"
            )
        ),
    ]

    expected_email_notifications = jobs.JobEmailNotifications.from_dict(
        databricks_run_config["email_notifications"]
    )
    expected_notification_settings = jobs.JobNotificationSettings.from_dict(
        databricks_run_config["notification_settings"]
    )
    expected_webhook_notifications = jobs.WebhookNotifications.from_dict(
        databricks_run_config["webhook_notifications"]
    )
    expected_job_health_settings = [
        jobs.JobsHealthRule.from_dict(j) for j in databricks_run_config["job_health_settings"]
    ]
    runner.submit_run(databricks_run_config, task)
    mock_submit_run.assert_called_once_with(
        run_name=databricks_run_config["run_name"],
        tasks=[expected_task],
        health=expected_job_health_settings,
        email_notifications=expected_email_notifications,
        notification_settings=expected_notification_settings,
        webhook_notifications=expected_webhook_notifications,
        idempotency_token=databricks_run_config["idempotency_token"],
        timeout_seconds=databricks_run_config["timeout_seconds"],
    )


def test_databricks_wait_for_run(mocker: MockerFixture):
    context = build_op_context()
    mock_submit_run = mocker.patch("databricks.sdk.JobsAPI.submit")
    mock_get_run = mocker.patch("databricks.sdk.JobsAPI.get_run")

    mock_submit_run_response = mock.Mock()
    mock_submit_run_response.bind.return_value = {"run_id": 1}
    mock_submit_run.return_value = mock_submit_run_response

    databricks_client = DatabricksClient(host=HOST, token=TOKEN)

    calls = {
        "num_calls": 0,
        "final_state": jobs.Run(
            state=jobs.RunState(
                result_state=DatabricksRunResultState.SUCCESS,
                life_cycle_state=DatabricksRunLifeCycleState.TERMINATED,
                state_message="Finished",
            ),
        ),
    }

    def _get_run(*args, **kwargs) -> dict:
        calls["num_calls"] += 1

        if calls["num_calls"] == 1:
            return jobs.Run(
                state=jobs.RunState(
                    life_cycle_state=DatabricksRunLifeCycleState.PENDING,
                    state_message="",
                ),
            )
        elif calls["num_calls"] == 2:
            return jobs.Run(
                state=jobs.RunState(
                    life_cycle_state=DatabricksRunLifeCycleState.RUNNING,
                    state_message="",
                ),
            )
        else:
            return calls["final_state"]

    mock_get_run.side_effect = _get_run

    databricks_client.wait_for_run_to_complete(
        logger=context.log,
        databricks_run_id=1,
        poll_interval_sec=0.01,
        max_wait_time_sec=10,
        verbose_logs=True,
    )

    calls["num_calls"] = 0
    calls["final_state"] = jobs.Run(
        state=jobs.RunState(
            result_state=None,
            life_cycle_state=DatabricksRunLifeCycleState.SKIPPED,
            state_message="Skipped",
        ),
    )

    databricks_client.wait_for_run_to_complete(
        logger=context.log,
        databricks_run_id=1,
        poll_interval_sec=0.01,
        max_wait_time_sec=10,
        verbose_logs=True,
    )

    calls["num_calls"] = 0
    calls["final_state"] = jobs.Run(
        state=jobs.RunState(
            result_state=DatabricksRunResultState.FAILED,
            life_cycle_state=DatabricksRunLifeCycleState.TERMINATED,
            state_message="Failed",
        ),
    )

    with pytest.raises(DatabricksError) as exc_info:
        databricks_client.wait_for_run_to_complete(
            logger=context.log,
            databricks_run_id=1,
            poll_interval_sec=0.01,
            max_wait_time_sec=10,
            verbose_logs=True,
        )

    assert "Run `1` failed with result state" in str(exc_info.value)


def test_dagster_databricks_user_agent() -> None:
    databricks_client = DatabricksClient(host=HOST, token=TOKEN)

    # TODO: Remove this once databricks_cli is removed
    assert "dagster-databricks" in databricks_client.api_client.default_headers["user-agent"]

    # TODO: Remove this once databricks_api is removed
    assert "dagster-databricks" in databricks_client.client.client.default_headers["user-agent"]

    assert "dagster-databricks" in databricks_client.workspace_client.config.user_agent
