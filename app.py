from __future__ import annotations

import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paramiko
import streamlit as st

st.set_page_config(page_title="SSH Remote Console", page_icon=":terminal:", layout="wide")

CONFIG_PATH = Path(os.environ.get("SSH_GUI_CONFIG", Path(__file__).with_name("config.json")))
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 10
DETACH_LOG_DIR = "$HOME/.ai-start/logs"
DETACH_HANDSHAKE_TIMEOUT = 15.0
LOG_TAIL_LINES = 120


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        st.error(f"File di configurazione non trovato: `{CONFIG_PATH}`")
        st.stop()
    except json.JSONDecodeError as e:
        st.error(f"Configurazione JSON non valida: {e}")
        st.stop()

    hosts = cfg.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        st.error("La configurazione deve contenere una lista non vuota 'hosts'.")
        st.stop()
    return cfg


def private_key_path(key_file: str) -> str:
    # config.json indica la chiave pubblica, ma paramiko carica solo quella privata.
    path = os.path.expanduser(key_file)
    if path.endswith(".pub"):
        return path[: -len(".pub")]
    return path


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def build_detached_command(command: str, host_id: str) -> tuple[str, str]:
    """Costruisce un comando remoto che avvia `command` come processo distaccato.

    Il processo viene sganciato dal canale SSH (stdin da /dev/null, stdout/stderr
    su file, SIGHUP ignorata da nohup), quindi sopravvive alla chiusura della
    sessione. Ritorna il comando da eseguire e il percorso del log remoto.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", host_id)
    name = f"{slug}-{stamp}.log"
    wrapped = (
        f'd="{DETACH_LOG_DIR}"; mkdir -p "$d" || exit 1; '
        f'nohup sh -c {sh_quote(command)} </dev/null >"$d/{name}" 2>&1 & '
        f'pid=$!; echo "$pid" >"$d/{name}.pid"; echo "PID=$pid LOG=$d/{name}"'
    )
    return wrapped, f"{DETACH_LOG_DIR}/{name}"


def _recv(fetch) -> bytes:
    try:
        return fetch(65536)
    except socket.timeout:
        return b""


def read_available(stdout, timeout: float) -> tuple[str, str, int | None]:
    """Legge stdout/stderr senza bloccare se il canale remoto resta aperto."""
    chan = stdout.channel
    chan.settimeout(0.1)
    deadline = time.time() + timeout
    out, err = bytearray(), bytearray()
    while True:
        chunk_out = _recv(chan.recv)
        chunk_err = _recv(chan.recv_stderr)
        out += chunk_out
        err += chunk_err
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.time() >= deadline:
            break
        if not (chunk_out or chunk_err):
            time.sleep(0.05)
    code = chan.recv_exit_status() if chan.exit_status_ready() else None
    return out.decode(errors="replace"), err.decode(errors="replace"), code


def connect(host_cfg: dict, password: str | None) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {
        "hostname": host_cfg["host"],
        "port": int(host_cfg.get("port", DEFAULT_PORT)),
        "username": host_cfg.get("user"),
        "timeout": int(host_cfg.get("timeout", DEFAULT_TIMEOUT)),
    }
    key_file = host_cfg.get("key_file")
    if key_file:
        kwargs["key_filename"] = private_key_path(key_file)
    if password:
        kwargs["password"] = password
    client.connect(**kwargs)
    return client


def run_ssh(host_cfg: dict, command: str, password: str | None, detach: bool = False) -> dict:
    """Esegue `command` via SSH su `host_cfg` e ritorna il risultato.

    Con `detach=True` il comando remoto viene distaccato dal canale: la funzione
    ritorna appena il processo è stato avviato e il processo continua a vivere
    anche dopo la chiusura della connessione SSH.
    """
    start = time.time()
    host_id = host_cfg.get("id") or host_cfg["host"]
    log_path = ""
    if detach:
        command, log_path = build_detached_command(command, host_id)
    result = {
        "ok": False,
        "code": None,
        "out": "",
        "err": "",
        "elapsed": 0.0,
        "detached": detach,
        "pid": "",
        "log": log_path,
    }
    client = None
    try:
        client = connect(host_cfg, password)
        _stdin, stdout, _stderr = client.exec_command(command)
        if detach:
            result["out"], result["err"], result["code"] = read_available(
                stdout, DETACH_HANDSHAKE_TIMEOUT
            )
            pid_match = re.search(r"PID=(\d+)", result["out"])
            log_match = re.search(r"LOG=(\S+)", result["out"])
            result["pid"] = pid_match.group(1) if pid_match else ""
            if log_match:
                result["log"] = log_match.group(1)
            result["ok"] = result["code"] == 0 and bool(result["pid"])
        else:
            result["out"] = stdout.read().decode(errors="replace")
            result["err"] = _stderr.read().decode(errors="replace")
            result["code"] = stdout.channel.recv_exit_status()
            result["ok"] = result["code"] == 0
    except Exception as e:
        result["err"] = f"{type(e).__name__}: {e}"
    finally:
        if client is not None:
            client.close()
    result["elapsed"] = time.time() - start
    return result


def fetch_log(host_cfg: dict, password: str | None, log_path: str) -> dict:
    # Nessun `--`: tail BSD (macOS) non lo garantisce, e il path quotato inizia sempre con /.
    return run_ssh(host_cfg, f"tail -n {LOG_TAIL_LINES} {sh_quote(log_path)}", password)


def render_result(result: dict) -> None:
    if result.get("code") is None:
        st.error(f"Errore di connessione/esecuzione: {result['err']}")
        return

    icon = "✅" if result["ok"] else "❌"
    if result.get("detached"):
        st.markdown(
            f"{icon} Processo distaccato — PID `{result['pid'] or '?'}` — "
            f"avviato in {result['elapsed']:.2f}s"
        )
        if result.get("log"):
            st.caption(f"Log remoto: `{result['log']}`")
        st.caption("L'exit code riguarda l'avvio: il comando remoto continua in background.")
    else:
        st.markdown(
            f"{icon} Exit code: **{result['code']}** — durata: {result['elapsed']:.2f}s"
        )

    with st.expander("Output (stdout)", expanded=not result.get("detached")):
        st.code(result["out"] or "(nessuna output)", language="bash")
    if result["err"]:
        with st.expander("Stderr", expanded=not result["ok"]):
            st.code(result["err"], language="bash")


def render_detached_log(
    result: dict, host_cfg: dict, password: str | None, uid: str
) -> None:
    log_path = result.get("log", "")
    if not (result.get("detached") and log_path.startswith("/")):
        return
    if st.button("Aggiorna log", key=f"log_{uid}", use_container_width=True):
        with st.spinner("Lettura log remoto..."):
            st.session_state[f"logtext_{uid}"] = fetch_log(host_cfg, password, log_path)
    log_result = st.session_state.get(f"logtext_{uid}")
    if log_result:
        with st.expander(f"Log — {log_path}", expanded=True):
            if log_result.get("code") is None:
                st.error(log_result["err"])
            else:
                st.code(log_result["out"] or "(log vuoto)", language="bash")


def host_panel(index: int, host_cfg: dict) -> None:
    host_id = host_cfg.get("id") or f"host-{index + 1}"
    uid = f"{host_id}#{index}"
    cmd_key = f"cmd_{uid}"
    pwd_key = f"pwd_{uid}"
    res_key = f"result_{uid}"
    detach_key = f"detach_{uid}"

    with st.container(border=True):
        st.subheader(host_cfg.get("label", host_cfg["host"]))
        st.caption(
            f"`{host_cfg.get('user')}@{host_cfg['host']}`:port {host_cfg.get('port', DEFAULT_PORT)}"
        )
        if host_cfg.get("key_file"):
            st.caption(
                f"Autenticazione: chiave `{private_key_path(host_cfg['key_file'])}`"
            )
        else:
            st.caption("Autenticazione: password (inserita di seguito)")

        st.text_input("Comando da eseguire", key=cmd_key, value=host_cfg.get("command", ""))
        st.checkbox(
            "Esegui in background (il processo remoto sopravvive alla chiusura SSH)",
            value=bool(host_cfg.get("detach", True)),
            key=detach_key,
        )
        use_password = not host_cfg.get("key_file")
        password = None
        if use_password:
            password = st.text_input(
                "Password", type="password", key=pwd_key, placeholder="(solo in memoria di sessione)"
            ) or None

        if st.button("Esegui via SSH", type="primary", key=f"run_{uid}", use_container_width=True):
            with st.spinner(f"Connessione a {host_cfg['host']}..."):
                st.session_state[res_key] = run_ssh(
                    host_cfg,
                    st.session_state[cmd_key],
                    password,
                    detach=bool(st.session_state[detach_key]),
                )

        result = st.session_state.get(res_key)
        if result:
            render_result(result)
            render_detached_log(result, host_cfg, password, uid)


def main() -> None:
    cfg = load_config()
    hosts = cfg["hosts"]

    st.title("SSH Remote Console")
    st.caption(
        f"Configurazione: `{CONFIG_PATH}` — {len(hosts)} host configurati. "
        "I comandi correnti sono quelli del file JSON, modificabili qui sopra. "
        "In modalità background il comando viene distaccato dalla sessione SSH."
    )

    if st.button("Esegui su tutti gli host", type="secondary", use_container_width=False):
        jobs = []
        for i, h in enumerate(hosts):
            uid = f"{h.get('id') or f'host-{i + 1}'}#{i}"
            cmd = st.session_state.get(f"cmd_{uid}", h.get("command", ""))
            password = st.session_state.get(f"pwd_{uid}") or None
            detach = bool(st.session_state.get(f"detach_{uid}", h.get("detach", True)))
            jobs.append((h, cmd, password, detach))
        with st.spinner("Esecuzione in parallelo..."):
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                results = list(pool.map(lambda j: run_ssh(*j), jobs))
        for i, (h, _cmd, _pwd, _detach) in enumerate(jobs):
            uid = f"{h.get('id') or f'host-{i + 1}'}#{i}"
            st.session_state[f"result_{uid}"] = results[i]

    if len(hosts) == 2:
        col1, col2 = st.columns(2)
        with col1:
            host_panel(0, hosts[0])
        with col2:
            host_panel(1, hosts[1])
    else:
        for i, h in enumerate(hosts):
            host_panel(i, h)

    res_keys = [
        k for k in st.session_state if k.startswith(("result_", "logtext_"))
    ]
    if res_keys and st.button("Pulisci risultati"):
        for k in res_keys:
            del st.session_state[k]
        st.rerun()


main()
