#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoveCar Lite 部署脚本（把代码推到装有 docker 的 NAS / 服务器上重建容器）

用法：
  export MOVECAR_HOST=192.168.1.10     # 目标机地址
  export MOVECAR_USER=admin            # SSH 账号
  export MOVECAR_PASS='你的SSH密码'
  python deploy/deploy.py --password '你的后台密码'   # 上传 + 构建 + 启动 + 自检
  python deploy/deploy.py --no-build                  # 只重启，不重建镜像
  python deploy/deploy.py --selftest                  # 只做远程自检

连接信息只从环境变量或命令行参数取，脚本里不含任何地址与口令：
  --host / MOVECAR_HOST、--user / MOVECAR_USER、MOVECAR_PASS（SSH 密码，仅供 sudo 用）
说明：只动 REMOTE 指向的那个目录（默认 /vol1/1000/docker/movecar_lite），不碰其他容器。
"""
import argparse
import io
import os
import posixpath
import sys
import time

import paramiko

HOST = os.environ.get("MOVECAR_HOST", "")
USER = os.environ.get("MOVECAR_USER", "")
PWD = os.environ.get("MOVECAR_PASS", "")
SUDO = PWD
REMOTE = "/vol1/1000/docker/movecar_lite"
LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["app.py", "Dockerfile", "docker-compose.yml"]
DIRS = ["static", "vendor"]
PORT = 13100


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PWD, timeout=15, banner_timeout=20, auth_timeout=20)
    return c


def run(c, cmd, sudo=False, timeout=900):
    """执行命令；sudo 用 -S 从 stdin 喂密码。"""
    full = f"sudo -S -p '' bash -c {shq(cmd)}" if sudo else cmd
    stdin, stdout, stderr = c.exec_command(full, timeout=timeout, get_pty=False)
    if sudo:
        stdin.write(SUDO + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def shq(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def upload(c, sftp, rel):
    local = os.path.join(LOCAL, rel.replace("/", os.sep))
    remote = posixpath.join(REMOTE, rel)
    if os.path.isdir(local):
        run(c, f"mkdir -p {shq(remote)}", sudo=True)
        for root, _dirs, files in os.walk(local):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".pyc"):
                    continue
                lp = os.path.join(root, f)
                rp = posixpath.join(REMOTE, os.path.relpath(lp, LOCAL).replace(os.sep, "/"))
                run(c, f"mkdir -p {shq(posixpath.dirname(rp))}", sudo=True)
                sftp.put(lp, rp)
                run(c, f"chmod 644 {shq(rp)}", sudo=True)
    else:
        sftp.put(local, remote)
        run(c, f"chmod 644 {shq(remote)}", sudo=True)
    log(f"↑ {rel}")


def main():
    global HOST, USER
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST, help="目标机地址，也可用 MOVECAR_HOST")
    ap.add_argument("--user", default=USER, help="SSH 账号，也可用 MOVECAR_USER")
    ap.add_argument("--password", default="", help="管理后台密码，写入 .env")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    HOST = (a.host or "").strip()
    USER = (a.user or "").strip()
    if not HOST or not USER:
        ap.error("缺少连接信息：请用 --host/--user 或环境变量 MOVECAR_HOST/MOVECAR_USER 指定")
    if not PWD:
        ap.error("缺少 SSH 密码：请设置环境变量 MOVECAR_PASS")

    c = connect()
    log(f"已连接 {HOST}")

    if not a.selftest:
        run(c, f"mkdir -p {REMOTE}/data", sudo=True)
        sftp = c.open_sftp()
        for rel in FILES + DIRS:
            upload(c, sftp, rel)
        if a.password:
            fd = io.BytesIO(f"ADMIN_PASSWORD={a.password}\nTZ=Asia/Shanghai\n".encode())
            sftp.putfo(fd, posixpath.join(REMOTE, ".env"))
            run(c, f"chmod 600 {shq(REMOTE + '/.env')}", sudo=True)
            log("↑ .env（密码已写入，权限 600）")
        else:
            code, out, _ = run(c, f"test -f {shq(REMOTE+'/.env')} && echo yes || echo no")
            if "no" in out:
                log("❌ 目标机没有 .env，请加 --password 首次初始化")
                sys.exit(1)

        if not a.no_build:
            log("构建镜像（约 10~30 秒）…")
            code, out, err = run(c, f"cd {REMOTE} && docker compose build", sudo=True)
            print(out[-2000:] or err[-2000:])
            if code != 0:
                log("❌ 构建失败")
                sys.exit(1)
        log("启动容器…")
        code, out, err = run(c, f"cd {REMOTE} && docker compose up -d --force-recreate", sudo=True)
        print(out[-2000:] or err[-2000:])
        if code != 0:
            log("❌ 启动失败")
            sys.exit(1)
        time.sleep(3)

    # 自检
    log("自检：容器状态")
    code, out, _ = run(c, "docker ps --filter name=movecar_lite --format '{{.Status}}'", sudo=True)
    print(out.strip())
    log("自检：HTTP")
    code, out, _ = run(
        c,
        f"curl -s -o /dev/null -w '%{{http_code}}' --noproxy '*' http://127.0.0.1:{PORT}/admin",
    )
    print("GET /admin  ->", out.strip())
    code, out, _ = run(
        c,
        f"curl -s --noproxy '*' http://127.0.0.1:{PORT}/api/pub?u=__probe__",
    )
    print("GET /api/pub ->", out.strip()[:160])
    log("自检：日志（末 15 行）")
    code, out, _ = run(c, "docker logs --tail 15 movecar_lite 2>&1", sudo=True)
    print(out.strip())
    c.close()
    log(f"完成：http://{HOST}:{PORT}")


if __name__ == "__main__":
    main()
