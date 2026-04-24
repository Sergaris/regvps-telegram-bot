"""Run ``mcops`` on the Minecraft host over OpenSSH."""

import asyncio
import logging
import shlex
from collections.abc import Sequence

import asyncssh

from vps_telegram_bot.config import McopsRemoteSettings

log = logging.getLogger(__name__)


def _posix_join_argv(argv: Sequence[str]) -> str:
    """Join argv for POSIX ``sh -c`` style (quoted)."""

    return " ".join(shlex.quote(part) for part in argv)


def _decode_process_output(value: str | bytes | None) -> str:
    """Normalize stdout/stderr from ``asyncssh`` process results."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


async def _run_remote_mcops_asyncssh(
    remote: McopsRemoteSettings,
    inner: str,
) -> tuple[int, str, str]:
    """Run remote shell command via AsyncSSH (password auth)."""

    password = remote.ssh_password
    if password is None:
        msg = "internal: asyncssh path requires ssh_password"
        raise RuntimeError(msg)
    conn_timeout = max(1, int(remote.timeout_sec))
    try:
        async with asyncssh.connect(
            remote.host,
            port=remote.port,
            username=remote.user,
            password=password,
            connect_timeout=conn_timeout,
        ) as conn:
            result = await conn.run(
                inner,
                check=False,
                timeout=remote.command_timeout_sec,
            )
    except TimeoutError:
        return (
            124,
            "",
            f"remote mcops timed out after {remote.command_timeout_sec:.0f}s",
        )
    except (OSError, asyncssh.Error) as e:
        log.warning("asyncssh failed for %s@%s", remote.user, remote.host)
        return 255, "", str(e)
    if result.exit_status is not None:
        code = int(result.exit_status)
    else:
        code = int(result.returncode or 0)
    out = _decode_process_output(result.stdout)
    err = _decode_process_output(result.stderr)
    return code, out, err


async def run_remote_mcops(remote: McopsRemoteSettings, argv: list[str]) -> tuple[int, str, str]:
    """Execute ``python -m mcops.cli <argv>`` on the remote host via SSH.

    Uses an SSH private key (``ssh -i``, batch mode) when ``identity_file`` is set,
    or password authentication via AsyncSSH when ``ssh_password`` is set.

    Args:
        remote: SSH and remote working directory settings.
        argv: Arguments after ``mcops`` (e.g. ``["status", "--json"]``).

    Returns:
        Tuple ``(exit_code, stdout, stderr)`` from the remote ``ssh`` process.
    """

    inner = (
        f"cd {shlex.quote(remote.remote_cwd)} && "
        f"{shlex.quote(remote.remote_python)} -m mcops.cli {_posix_join_argv(argv)}"
    )
    if remote.ssh_password is not None:
        log.info(
            "remote mcops (password auth): ssh %s@%s … %s",
            remote.user,
            remote.host,
            " ".join(argv[:6]),
        )
        return await _run_remote_mcops_asyncssh(remote, inner)

    identity = remote.identity_file
    if identity is None:
        msg = "internal: McopsRemoteSettings without password must have identity_file"
        raise RuntimeError(msg)
    cmd: list[str] = [
        "ssh",
        "-p",
        str(remote.port),
        "-i",
        identity,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(remote.timeout_sec))}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{remote.user}@{remote.host}",
        inner,
    ]
    log.info("remote mcops: ssh %s@%s … %s", remote.user, remote.host, " ".join(argv[:6]))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=remote.command_timeout_sec,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"remote mcops timed out after {remote.command_timeout_sec:.0f}s"
    code = int(proc.returncode or 0)
    out = (out_b or b"").decode("utf-8", errors="replace")
    err = (err_b or b"").decode("utf-8", errors="replace")
    return code, out, err
