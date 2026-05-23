# scripts/

可双击或一行命令运行的脚本。详细说明见 [`docs/running.md`](../docs/running.md)。

## Windows

| 脚本 | 用途 |
|---|---|
| `run.bat` | 启动 Web 后端 (http://127.0.0.1:6789/) |
| `run-dev.bat` | 启动 Web 后端,代码改动自动 reload |
| `test.bat` | 跑全部测试 |
| `lint.bat` | 跑 ruff 检查 (不会自动修改文件) |

双击即可。

## macOS / Linux

```bash
chmod +x scripts/*.sh
./scripts/run.sh
./scripts/run-dev.sh
./scripts/test.sh
./scripts/lint.sh
```

## 自定义环境变量

所有脚本默认在 `omnicode-env` conda 环境里跑,Web 后端默认 6789 端口。
要改的话:

```cmd
:: Windows
set CONDA_ENV_NAME=my-env
set PORT=7000
scripts\run.bat
```

```bash
# macOS / Linux
CONDA_ENV_NAME=my-env PORT=7000 ./scripts/run.sh
```

## 不用 conda

如果你用 `venv` 而不是 conda,脚本不能直接用——它们用 `conda run` 调度。
最简单的办法是激活 venv 后手动跑命令,见 `docs/running.md`。
