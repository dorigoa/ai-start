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
PIDS_PATH = Path(os.environ.get("SSH_GUI_PIDS", Path(__file__).with_name("processes.json")))
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


def load_process_records() -> dict:
    try:
        with open(PIDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_process_records(records: dict) -> None:
    with open(PIDS_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2, sort_keys=True)


def record_detached_start(host_id: str, host_cfg: dict, result: dict, command: str) -> None:
    """Persiste il PID di un processo distaccato avviato con successo."""
    if not (result.get("detached") and result.get("ok") and result.get("pid")):
        return
    records = load_process_records()
    records[host_id] = {
        "pid": result["pid"],
        "host": host_cfg["host"],
        "user": host_cfg.get("user"),
        "command": command,
        "log": result.get("log", ""),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_process_records(records)


def forget_process(host_id: str) -> None:
    records = load_process_records()
    if records.pop(host_id, None) is not None:
        save_process_records(records)


def kill_remote(host_cfg: dict, password: str | None, pid: str) -> dict:
    """Termina l'intero albero di processi remoto (PID radice e tutti i discendenti).

    Il comando lanciato in background spesso è un wrapper che forka il processo
    reale: si raccoglie quindi il ramo completo con `pgrep -P` ricorsivo, si invia
    SIGTERM a tutti, poi SIGKILL a chi resiste. Nota: non copre i processi che si
    sono daemonizzati (padre uscito, figli orfani riattaccati a init).
    """
    q = sh_quote(pid)
    # al(ivo): `kill -0` risponde bene anche ai zombie, quindi si controlla lo
    # stato reale con ps (Z = zombie, da considerare morto).
    cmd = (
        f"if ! kill -0 {q} 2>/dev/null; then echo 'NOPROCESS'; exit 0; fi; "
        f"al(){{ s=$(ps -o state= -p \"$1\" 2>/dev/null | tr -d ' '); "
        f"[ -n \"$s\" ] && [ \"$s\" != \"Z\" ]; }}; "
        f"c(){{ echo \"$1\"; for x in $(pgrep -P \"$1\" 2>/dev/null); do c \"$x\"; done; }}; "
        f"all=$(c {q}); "
        f"for p in $all; do kill -TERM \"$p\" 2>/dev/null; done; "
        f"sleep 0.5; "
        f"for p in $all; do al \"$p\" && kill -KILL \"$p\" 2>/dev/null; done; "
        f"sleep 0.3; "
        f"left=0; for p in $all; do al \"$p\" && left=1; done; "
        f"[ \"$left\" -eq 0 ] && {{ echo 'KILLED'; exit 0; }} || {{ echo 'STILL_ALIVE'; exit 1; }}"
    )
    return run_ssh(host_cfg, cmd, password)


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


def render_kill_section(
    host_id: str, host_cfg: dict, password: str | None, uid: str
) -> None:
    record = load_process_records().get(host_id)
    kill_res = st.session_state.get(f"killres_{uid}")

    if not record and not kill_res:
        return

    if record and record.get("pid"):
        pid = record["pid"]
        st.markdown(
            f"Processo remoto tracciato — PID `{pid}` — avviato {record.get('started', '?')}"
        )
        st.caption("Il kill termina anche tutti i processi figli/collegati (albero completo).")
        if record.get("command"):
            st.caption(f"Comando: `{record['command']}`")

        if st.button("⛔ Kill processo remoto", key=f"kill_{uid}", use_container_width=True):
            with st.spinner(f"Termino l'albero di processi (PID {pid}) su {host_cfg['host']}..."):
                res = kill_remote(host_cfg, password, pid)
            st.session_state[f"killres_{uid}"] = {"res": res, "pid": pid}
            if res.get("code") == 0 and "STILL_ALIVE" not in res.get("out", ""):
                forget_process(host_id)
            st.rerun()

    if kill_res:
        pid = kill_res.get("pid", "?")
        res = kill_res["res"]
        if res.get("code") is None:
            st.error(f"Errore kill: {res['err']}")
        elif "STILL_ALIVE" in res.get("out", ""):
            st.error(f"Impossibile terminare il PID {pid} anche con SIGKILL.")
        elif "NOPROCESS" in res.get("out", ""):
            st.warning(f"Il PID {pid} non è attivo sul nodo remoto.")
            forget_process(host_id)
        else:
            st.success(f"Processo remoto terminato (PID {pid}).")


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
                result = run_ssh(
                    host_cfg,
                    st.session_state[cmd_key],
                    password,
                    detach=bool(st.session_state[detach_key]),
                )
            st.session_state[res_key] = result
            record_detached_start(host_id, host_cfg, result, st.session_state[cmd_key])
            if result.get("detached") and result.get("ok") and result.get("pid"):
                st.session_state.pop(f"killres_{uid}", None)

        result = st.session_state.get(res_key)
        if result:
            render_result(result)
            render_detached_log(result, host_cfg, password, uid)
        render_kill_section(host_id, host_cfg, password, uid)


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
        for i, (h, cmd, _pwd, _detach) in enumerate(jobs):
            host_id = h.get("id") or f"host-{i + 1}"
            uid = f"{host_id}#{i}"
            st.session_state[f"result_{uid}"] = results[i]
            record_detached_start(host_id, h, results[i], cmd)

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
        k for k in st.session_state if k.startswith(("result_", "logtext_", "killres_"))
    ]
    if res_keys and st.button("Pulisci risultati"):
        for k in res_keys:
            del st.session_state[k]
        st.rerun()


main()
