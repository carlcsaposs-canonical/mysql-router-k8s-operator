# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import itertools
import json
import logging
import pathlib
import subprocess
import tempfile
from typing import Dict, List, Optional

import mysql.connector
import tenacity
import yaml
from juju.model import Model
from juju.unit import Unit
from mysql.connector.errors import (
    DatabaseError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from pytest_operator.plugin import OpsTest

from .connector import MySQLConnector
from .juju_ import run_action

logger = logging.getLogger(__name__)

CONTINUOUS_WRITES_DATABASE_NAME = "continuous_writes_database"
CONTINUOUS_WRITES_TABLE_NAME = "data"

MYSQL_DEFAULT_APP_NAME = "mysql-k8s"
MYSQL_ROUTER_DEFAULT_APP_NAME = "mysql-router-k8s"
APPLICATION_DEFAULT_APP_NAME = "mysql-test-app"

SERVER_CONFIG_USERNAME = "serverconfig"
CONTAINER_NAME = "mysql-router"
LOGROTATE_EXECUTOR_SERVICE = "logrotate_executor"


async def execute_queries_against_unit(
    unit_address: str,
    username: str,
    password: str,
    queries: List[str],
    commit: bool = False,
) -> List:
    """Execute given MySQL queries on a unit.

    Args:
        unit_address: The public IP address of the unit to execute the queries on
        username: The MySQL username
        password: The MySQL password
        queries: A list of queries to execute
        commit: A keyword arg indicating whether there are any writes queries

    Returns:
        A list of rows that were potentially queried
    """
    connection = mysql.connector.connect(
        host=unit_address,
        user=username,
        password=password,
    )
    cursor = connection.cursor()

    for query in queries:
        cursor.execute(query)

    if commit:
        connection.commit()

    output = list(itertools.chain(*cursor.fetchall()))

    cursor.close()
    connection.close()

    return output


async def get_server_config_credentials(unit: Unit) -> Dict:
    """Helper to run an action to retrieve server config credentials.

    Args:
        unit: The juju unit on which to run the get-password action for server-config credentials

    Returns:
        A dictionary with the server config username and password
    """
    return await run_action(unit, "get-password", username=SERVER_CONFIG_USERNAME)


async def get_inserted_data_by_application(unit: Unit) -> Optional[str]:
    """Helper to run an action to retrieve inserted data by the application.

    Args:
        unit: The juju unit on which to run the get-inserted-data action

    Returns:
        A string representing the inserted data
    """
    return (await run_action(unit, "get-inserted-data")).get("data")


async def get_credentials(unit: Unit) -> Dict:
    """Helper to run an action on data-integrator to get credentials.

    Args:
        unit: The data-integrator unit to run action against

    Returns:
        A dictionary with the credentials
    """
    return await run_action(unit, "get-credentials")


async def get_unit_address(ops_test: OpsTest, unit_name: str) -> str:
    """Get unit IP address.

    Args:
        ops_test: The ops test framework instance
        unit_name: The name of the unit

    Returns:
        IP address of the unit
    """
    status = await ops_test.model.get_status()
    return status["applications"][unit_name.split("/")[0]].units[unit_name]["address"]


async def scale_application(
    ops_test: OpsTest, application_name: str, desired_count: int, wait: bool = True
) -> None:
    """Scale a given application to the desired unit count.

    Args:
        ops_test: The ops test framework
        application_name: The name of the application
        desired_count: The number of units to scale to
        wait: Boolean indicating whether to wait until units
            reach desired count
    """
    await ops_test.model.applications[application_name].scale(desired_count)

    if desired_count > 0 and wait:
        await ops_test.model.wait_for_idle(
            apps=[application_name],
            status="active",
            timeout=(15 * 60),
            wait_for_exact_units=desired_count,
        )


async def delete_file_or_directory_in_unit(
    ops_test: OpsTest, unit_name: str, path: str, container_name: str = CONTAINER_NAME
) -> bool:
    """Delete a file in the provided unit.

    Args:
        ops_test: The ops test framework
        unit_name: The name unit on which to delete the file from
        container_name: The name of the container where the file or directory is
        path: The path of file or directory to delete

    Returns:
        boolean indicating success
    """
    if path.strip() in ["/", "."]:
        return

    await ops_test.juju(
        "ssh",
        "--container",
        container_name,
        unit_name,
        "find",
        path,
        "-maxdepth",
        "1",
        "-delete",
    )


async def get_process_pid(
    ops_test: OpsTest, unit_name: str, container_name: str, process: str
) -> int:
    """Return the pid of a process running in a given unit.

    Args:
        ops_test: The ops test object passed into every test case
        unit_name: The name of the unit
        container_name: The name of the container to get the process pid from
        process: The process name to search for
    Returns:
        A integer for the process id
    """
    try:
        _, raw_pid, _ = await ops_test.juju("ssh", unit_name, "pgrep", "-x", process)
        pid = int(raw_pid.strip())

        return pid
    except Exception:
        return None


async def write_content_to_file_in_unit(
    ops_test: OpsTest, unit: Unit, path: str, content: str, container_name: str = CONTAINER_NAME
) -> None:
    """Write content to the file in the provided unit.

    Args:
        ops_test: The ops test framework
        unit: THe unit in which to write to file in
        path: The path at which to write the content to
        content: The content to write to the file
        container_name: The container where to write the file
    """
    pod_name = unit.name.replace("/", "-")

    with tempfile.NamedTemporaryFile(mode="w", dir=pathlib.Path.home()) as temp_file:
        temp_file.write(content)
        temp_file.flush()

        subprocess.run(
            [
                "microk8s.kubectl",
                "cp",
                "-n",
                ops_test.model.info.name,
                "-c",
                container_name,
                temp_file.name,
                f"{pod_name}:{path}",
            ],
            check=True,
        )


async def read_contents_from_file_in_unit(
    ops_test: OpsTest, unit: Unit, path: str, container_name: str = CONTAINER_NAME
) -> str:
    """Read contents from file in the provided unit.

    Args:
        ops_test: The ops test framework
        unit: The unit in which to read file from
        path: The path from which to read content from
        container_name: The container where the file exists

    Returns:
        the contents of the file
    """
    pod_name = unit.name.replace("/", "-")

    with tempfile.NamedTemporaryFile(mode="r+", dir=pathlib.Path.home()) as temp_file:
        subprocess.run(
            [
                "microk8s.kubectl",
                "cp",
                "-n",
                ops_test.model.info.name,
                "-c",
                container_name,
                f"{pod_name}:{path}",
                temp_file.name,
            ],
            check=True,
        )

        temp_file.seek(0)

        contents = ""
        for line in temp_file:
            contents += line
            contents += "\n"

    return contents


async def ls_la_in_unit(
    ops_test: OpsTest, unit_name: str, directory: str, container_name: str = CONTAINER_NAME
) -> list[str]:
    """Returns the output of ls -la in unit.

    Args:
        ops_test: The ops test framework
        unit_name: The name of unit in which to run ls -la
        directory: The directory from which to run ls -la
        container_name: The container where to run ls -la

    Returns:
        a list of files returned by ls -la
    """
    return_code, output, _ = await ops_test.juju(
        "ssh", "--container", container_name, unit_name, "ls", "-la", directory
    )
    assert return_code == 0

    ls_output = output.split("\n")[1:]

    return [
        line.strip("\r")
        for line in ls_output
        if len(line.strip()) > 0 and line.split()[-1] not in [".", ".."]
    ]


async def stop_running_log_rotate_executor(ops_test: OpsTest, unit_name: str):
    """Stop running the log rotate executor script.

    Args:
        ops_test: The ops test object passed into every test case
        unit_name: The name of the unit to be tested
    """
    # send KILL signal to log rotate executor, which trigger shutdown process
    await ops_test.juju(
        "ssh",
        "--container",
        CONTAINER_NAME,
        unit_name,
        "pebble",
        "stop",
        LOGROTATE_EXECUTOR_SERVICE,
    )


async def stop_running_flush_mysqlrouter_job(ops_test: OpsTest, unit_name: str) -> None:
    """Stop running any logrotate jobs that may have been triggered by cron.

    Args:
        ops_test: The ops test object passed into every test case
        unit_name: The name of the unit to be tested
    """
    # send KILL signal to log rotate process, which trigger shutdown process
    await ops_test.juju(
        "ssh",
        "--container",
        CONTAINER_NAME,
        unit_name,
        "pkill",
        "-9",
        "-f",
        "logrotate -f /etc/logrotate.d/flush_mysqlrouter_logs",
    )

    # hold execution until process is stopped
    for attempt in tenacity.Retrying(
        reraise=True, stop=tenacity.stop_after_attempt(45), wait=tenacity.wait_fixed(2)
    ):
        with attempt:
            if await get_process_pid(ops_test, unit_name, CONTAINER_NAME, "logrotate"):
                raise Exception("Failed to stop the flush_mysql_logs logrotate process.")


async def rotate_mysqlrouter_logs(ops_test: OpsTest, unit_name: str) -> None:
    """Dispatch the custom event to run logrotate.

    Args:
        ops_test: The ops test object passed into every test case
        unit_name: The name of the unit to be tested
    """
    pod_label = unit_name.replace("/", "-")

    subprocess.run(
        [
            "microk8s.kubectl",
            "exec",
            "-n",
            ops_test.model.info.name,
            "-it",
            pod_label,
            "--container",
            CONTAINER_NAME,
            "--",
            "su",
            "-",
            "mysql",
            "-c",
            "logrotate -f -s /tmp/logrotate.status /etc/logrotate.d/flush_mysqlrouter_logs",
        ],
        check=True,
    )


@tenacity.retry(stop=tenacity.stop_after_attempt(8), wait=tenacity.wait_fixed(15), reraise=True)
def is_connection_possible(credentials: Dict, **extra_opts) -> bool:
    """Test a connection to a MySQL server.

    Args:
        credentials: A dictionary with the credentials to test
        extra_opts: extra options for mysql connection
    """
    config = {
        "user": credentials["username"],
        "password": credentials["password"],
        "host": credentials["host"],
        "raise_on_warnings": False,
        "connection_timeout": 10,
        **extra_opts,
    }
    try:
        with MySQLConnector(config) as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone()[0] == 1
    except (DatabaseError, InterfaceError, OperationalError, ProgrammingError) as e:
        # Errors raised when the connection is not possible
        logger.error(e)
        return False


async def get_tls_ca(
    ops_test: OpsTest,
    unit_name: str,
) -> str:
    """Returns the TLS CA used by the unit.

    Args:
        ops_test: The ops test framework instance
        unit_name: The name of the unit

    Returns:
        TLS CA or an empty string if there is no CA.
    """
    raw_data = (await ops_test.juju("show-unit", unit_name))[1]
    if not raw_data:
        raise ValueError(f"no unit info could be grabbed for {unit_name}")
    data = yaml.safe_load(raw_data)
    # Filter the data based on the relation name.
    relation_data = [
        v for v in data[unit_name]["relation-info"] if v["endpoint"] == "certificates"
    ]
    if len(relation_data) == 0:
        return ""
    return json.loads(relation_data[0]["application-data"]["certificates"])[0].get("ca")


async def get_tls_certificate_issuer(
    ops_test: OpsTest,
    unit_name: str,
    socket: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> str:
    connect_args = f"-unix {socket}" if socket else f"-connect {host}:{port}"
    get_tls_certificate_issuer_commands = [
        "ssh",
        "--container",
        CONTAINER_NAME,
        unit_name,
        f"openssl s_client -showcerts -starttls mysql {connect_args} < /dev/null | openssl x509 -text | grep Issuer",
    ]
    return_code, issuer, _ = await ops_test.juju(*get_tls_certificate_issuer_commands)
    assert return_code == 0, f"failed to get TLS certificate issuer on {unit_name=}"
    return issuer


def get_application_name(ops_test: OpsTest, application_name_substring: str) -> str:
    """Returns the name of the application with the provided application name.

    This enables us to retrieve the name of the deployed application in an existing model.

    Note: if multiple applications with the application name exist,
    the first one found will be returned.
    """
    for application in ops_test.model.applications:
        if application_name_substring == application:
            return application

    return ""


@tenacity.retry(stop=tenacity.stop_after_attempt(30), wait=tenacity.wait_fixed(5), reraise=True)
async def get_primary_unit(
    ops_test: OpsTest,
    unit: Unit,
    app_name: str,
) -> Unit:
    """Helper to retrieve the primary unit.

    Args:
        ops_test: The ops test object passed into every test case
        unit: A unit on which to run dba.get_cluster().status() on
        app_name: The name of the test application
        cluster_name: The name of the test cluster

    Returns:
        A juju unit that is a MySQL primary
    """
    units = ops_test.model.applications[app_name].units
    results = await run_action(unit, "get-cluster-status")

    primary_unit = None
    for k, v in results["status"]["defaultreplicaset"]["topology"].items():
        if v["memberrole"] == "primary":
            unit_name = f"{app_name}/{k.split('-')[-1]}"
            primary_unit = [unit for unit in units if unit.name == unit_name][0]
            break

    if not primary_unit:
        raise ValueError("Unable to find primary unit")
    return primary_unit


async def get_primary_unit_wrapper(ops_test: OpsTest, app_name: str, unit_excluded=None) -> Unit:
    """Wrapper for getting primary.

    Args:
        ops_test: The ops test object passed into every test case
        app_name: The name of the application
        unit_excluded: excluded unit to run command on
    Returns:
        The primary Unit object
    """
    logger.info("Retrieving primary unit")
    units = ops_test.model.applications[app_name].units
    if unit_excluded:
        # if defined, exclude unit from available unit to run command on
        # useful when the workload is stopped on unit
        unit = ({unit for unit in units if unit.name != unit_excluded.name}).pop()
    else:
        unit = units[0]

    primary_unit = await get_primary_unit(ops_test, unit, app_name)

    return primary_unit


async def get_max_written_value_in_database(
    ops_test: OpsTest, unit: Unit, credentials: dict
) -> int:
    """Retrieve the max written value in the MySQL database.

    Args:
        ops_test: The ops test framework
        unit: The MySQL unit on which to execute queries on
        credentials: Database credentials to use
    """
    unit_address = await get_unit_address(ops_test, unit.name)

    select_max_written_value_sql = [
        f"SELECT MAX(number) FROM `{CONTINUOUS_WRITES_DATABASE_NAME}`.`{CONTINUOUS_WRITES_TABLE_NAME}`;"
    ]

    output = await execute_queries_against_unit(
        unit_address,
        credentials["username"],
        credentials["password"],
        select_max_written_value_sql,
    )

    return output[0]


async def ensure_all_units_continuous_writes_incrementing(
    ops_test: OpsTest, mysql_units: Optional[List[Unit]] = None
) -> None:
    """Ensure that continuous writes is incrementing on all units.

    Also, ensure that all continuous writes up to the max written value is available
    on all units (ensure that no committed data is lost).
    """
    logger.info("Ensure continuous writes are incrementing")

    mysql_application_name = get_application_name(ops_test, MYSQL_DEFAULT_APP_NAME)

    if not mysql_units:
        mysql_units = ops_test.model.applications[mysql_application_name].units

    primary = await get_primary_unit_wrapper(ops_test, mysql_application_name)

    server_config_credentials = await get_server_config_credentials(mysql_units[0])

    last_max_written_value = await get_max_written_value_in_database(
        ops_test, primary, server_config_credentials
    )

    select_all_continuous_writes_sql = [
        f"SELECT * FROM `{CONTINUOUS_WRITES_DATABASE_NAME}`.`{CONTINUOUS_WRITES_TABLE_NAME}`"
    ]

    async with ops_test.fast_forward():
        for unit in mysql_units:
            for attempt in tenacity.Retrying(
                reraise=True, stop=tenacity.stop_after_delay(5 * 60), wait=tenacity.wait_fixed(10)
            ):
                with attempt:
                    # ensure that all units are up to date (including the previous primary)
                    unit_address = await get_unit_address(ops_test, unit.name)

                    # ensure the max written value is incrementing (continuous writes is active)
                    max_written_value = await get_max_written_value_in_database(
                        ops_test, unit, server_config_credentials
                    )
                    assert (
                        max_written_value > last_max_written_value
                    ), "Continuous writes not incrementing"

                    # ensure that the unit contains all values up to the max written value
                    all_written_values = set(
                        await execute_queries_against_unit(
                            unit_address,
                            server_config_credentials["username"],
                            server_config_credentials["password"],
                            select_all_continuous_writes_sql,
                        )
                    )
                    numbers = set(range(1, max_written_value))
                    assert (
                        numbers <= all_written_values
                    ), f"Missing numbers in database for unit {unit.name}"

                    last_max_written_value = max_written_value


async def get_workload_version(ops_test: OpsTest, unit_name: str) -> str:
    """Get the workload version of the deployed router charm."""
    return_code, output, _ = await ops_test.juju(
        "ssh",
        unit_name,
        "sudo",
        "cat",
        f"/var/lib/juju/agents/unit-{unit_name.replace('/', '-')}/charm/workload_version",
    )

    assert return_code == 0
    return output.strip()


async def get_leader_unit(
    ops_test: Optional[OpsTest], app_name: str, model: Optional[Model] = None
) -> Optional[Unit]:
    """Get the leader unit of a given application.

    Args:
        ops_test: The ops test framework instance
        app_name: The name of the application
        model: The model to use (overrides ops_test.model)
    """
    leader_unit = None
    if not model:
        model = ops_test.model
    for unit in model.applications[app_name].units:
        if await unit.is_leader_from_status():
            leader_unit = unit
            break

    return leader_unit


def get_juju_status(model_name: str) -> str:
    """Return the juju status output.

    Args:
        model_name: The model for which to retrieve juju status for
    """
    return subprocess.check_output(["juju", "status", "--model", model_name]).decode("utf-8")
