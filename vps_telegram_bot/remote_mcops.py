"""Run ``mcops`` on the Minecraft host over OpenSSH."""

import asyncio
import logging
import shlex
from collections.abc import Sequence

from vps_telegram_bot.config import McopsRemoteSettings

log = logging.getLogger(__name__)


def _posix_join_argv(argv: Sequence[str]) -> str:
    """Join argv for POSIX ``sh -c`` style (quoted)."""

    return " ".join(shlex.quote(part) for part in argv)


async def run_remote_mcops(remote: McopsRemoteSettings, argv: list[str]) -> tuple[int, str, str]:
    """Execute ``python -m mcops.cli <argv>`` on the remote host via SSH.

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
    cmd: list[str] = [
        "ssh",
        "-p",
        str(remote.port),
        "-i",
        remote.identity_file,
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
