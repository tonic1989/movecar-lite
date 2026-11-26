#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 movecar_lite 打成 fnOS 可安装的 fpk 包（安装时可自定义端口与密码）。

用法：
    python build.py                     # 本机用 fnpack.exe 打包（快，但 cmd/ 权限位可能丢）
    python build.py --remote            # 上传到 fnOS 用官方 fnpack 打包（推荐，权限位正确）
    python build.py --remote --keep     # 保留 staging 便于排查

--remote 需要 paramiko，并要知道目标机的地址与账号：用 --host/--user/--password 传，
或设环境变量 MOVECAR_HOST / MOVECAR_USER / MOVECAR_PASS（脚本里不含任何地址与口令）。
产物放到 ../outputs/movecarlite_<version>_<platform>.fpk
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)          # movecar_lite/
STAGE_ROOT = os.path.join(HERE, "build")
APP_NAME = "movecarlite"
OUT_DIR = os.path.join(PROJ, "outputs")

APP_FILES = ["app.py"]
APP_DIRS = ["static", "vendor"]
TEXT_EXT = {".py", ".html", ".css", ".json", ".js", ".md"}

DEFAULT_HOST = os.environ.get("MOVECAR_HOST", "")
DEFAULT_USER = os.environ.get("MOVECAR_USER", "")
DEFAULT_PASS = os.environ.get("MOVECAR_PASS", "")


# --------------------------------------------------------------------------- 工具
def read_manifest():
    meta = {}
    with open(os.path.join(HERE, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
    return meta


def fnpack_bin():
    for name in ("fnpack", "fnpack.exe"):
        p = os.path.join(HERE, name)
        if os.path.isfile(p):
            return p
    raise SystemExit("找不到 fnpack，请把官方 fnpack 放到 " + HERE)


def lf(path):
    """把文本文件的 CRLF 统一成 LF（否则 Linux 上脚本会报 $'\\r' 错误）。"""
    with open(path, "rb") as f:
        data = f.read()
    fixed = data.replace(b"\r\n", b"\n")
    if fixed != data:
        with open(path, "wb") as f:
            f.write(fixed)


def normalize_tree(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).split(os.sep)
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if os.path.splitext(fn)[1].lower() in TEXT_EXT or "cmd" in rel or fn == "manifest":
                lf(p)


# --------------------------------------------------------------------------- 暂存
def stage():
    if os.path.isdir(STAGE_ROOT):
        shutil.rmtree(STAGE_ROOT)
    app_root = os.path.join(STAGE_ROOT, APP_NAME)
    os.makedirs(app_root)

    for item in ("manifest", "ICON.PNG", "ICON_256.PNG"):
        src = os.path.join(HERE, item)
        if not os.path.isfile(src):
            raise SystemExit("缺少文件：" + src + "（图标可用 make_icons.ps1 生成）")
        shutil.copy2(src, os.path.join(app_root, item))

    for d in ("cmd", "config", "wizard"):
        shutil.copytree(os.path.join(HERE, d), os.path.join(app_root, d))

    app_dest = os.path.join(app_root, "app")
    os.makedirs(app_dest)
    for f in APP_FILES:
        shutil.copy2(os.path.join(PROJ, f), os.path.join(app_dest, f))
    for d in APP_DIRS:
        shutil.copytree(os.path.join(PROJ, d), os.path.join(app_dest, d),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"))
    shutil.copytree(os.path.join(HERE, "app", "ui"), os.path.join(app_dest, "ui"))

    for dirpath, dirnames, _f in os.walk(app_root):
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"))
            dirnames.remove("__pycache__")
    normalize_tree(app_root)

    for script in os.listdir(os.path.join(app_root, "cmd")):
        os.chmod(os.path.join(app_root, "cmd", script), 0o755)
    return app_root


def out_name(meta):
    return "%s_%s_%s.fpk" % (meta.get("appname", APP_NAME), meta.get("version", "0.0.0"),
                             meta.get("platform", "all"))


# --------------------------------------------------------------------------- 本机打包
def build_local(app_root, meta):
    os.makedirs(OUT_DIR, exist_ok=True)
    cmd = [fnpack_bin(), "build", "--directory", app_root]
    print("$ " + " ".join(cmd))
    res = subprocess.run(cmd, cwd=OUT_DIR, capture_output=True, text=True)
    print(((res.stdout or "") + (res.stderr or "")).strip())
    if res.returncode != 0:
        raise SystemExit("fnpack build 失败，退出码 %s" % res.returncode)
    src = os.path.join(OUT_DIR, APP_NAME + ".fpk")
    dst = os.path.join(OUT_DIR, out_name(meta))
    if os.path.isfile(dst):
        os.remove(dst)
    if os.path.isfile(src):
        os.rename(src, dst)
        return dst
    raise SystemExit("打包完成但没找到 .fpk 产物")


# --------------------------------------------------------------------------- 远端打包
def build_remote(app_root, meta, host, user, password):
    import paramiko

    os.makedirs(OUT_DIR, exist_ok=True)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, 22, user, password, timeout=15)

    def sh(cmd, timeout=300):
        wrapped = "echo '%s' | sudo -S bash -c %s" % (password, "'" + cmd.replace("'", "'\\''") + "'")
        _i, out, err = cli.exec_command(wrapped, timeout=timeout)
        text = (out.read() + err.read()).decode(errors="replace")
        # 去掉 sudo 提示与登录 shell 的 chdir 噪声
        keep = [ln for ln in text.splitlines()
                if not ln.startswith("[sudo] password") and "Could not chdir to home directory" not in ln]
        return "\n".join(keep)

    remote_dir = "/tmp/fpkbuild_" + APP_NAME
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(app_root, arcname=APP_NAME)

    sftp = cli.open_sftp()
    sftp.putfo(io.BytesIO(buf.getvalue()), "/tmp/%s_stage.tgz" % APP_NAME)
    sftp.close()

    try:
        print(sh("rm -rf %s && mkdir -p %s" % (remote_dir, remote_dir)))
        print(sh("tar xzf /tmp/%s_stage.tgz -C %s" % (APP_NAME, remote_dir)))
        # Linux 上才有的执行位：目录 755 / 文件 644 / cmd 脚本 755
        print(sh("find {d}/ -type d -exec chmod 755 {{}} + ; "
                 "find {d}/ -type f -exec chmod 644 {{}} + ; "
                 "chmod 755 {d}/{a}/cmd/* ; ls -l {d}/{a}/cmd/main".format(d=remote_dir, a=APP_NAME)))
        res = sh("cd {d} && rm -f *.fpk && /usr/local/bin/fnpack build --directory {d}/{a} 2>&1 | tail -5"
                 .format(d=remote_dir, a=APP_NAME))
        print(res.strip())
        remote_fpk = "%s/%s.fpk" % (remote_dir, APP_NAME)
        check = sh("test -f %s && echo __OK__ || echo __MISSING__" % remote_fpk)
        print(check.strip())
        if "__MISSING__" in check or "__OK__" not in check:
            raise SystemExit("远端打包失败")

        local_fpk = os.path.join(OUT_DIR, out_name(meta))
        sftp = cli.open_sftp()
        sftp.get(remote_fpk, local_fpk)
        sftp.close()
    finally:
        cli.close()
    return local_fpk


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true", help="上传到 fnOS 用官方 fnpack 打包")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=DEFAULT_PASS)
    ap.add_argument("--keep", action="store_true", help="保留 staging 目录")
    args = ap.parse_args()

    meta = read_manifest()
    app_root = stage()
    print("staging: " + app_root)

    if args.remote:
        if not args.host or not args.user or not args.password:
            ap.error("--remote 需要 --host/--user/--password（或 MOVECAR_HOST/MOVECAR_USER/MOVECAR_PASS）")
        fpk = build_remote(app_root, meta, args.host, args.user, args.password)
    else:
        fpk = build_local(app_root, meta)

    print("\n✅ 产物: %s (%.1f KB)" % (fpk, os.path.getsize(fpk) / 1024.0))
    if not args.keep:
        shutil.rmtree(STAGE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
