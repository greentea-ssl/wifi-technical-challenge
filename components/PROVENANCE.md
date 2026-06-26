# components/ — 取り込み元（self-contained 用スナップショット）

提出を self-contained にするため、参照リポジトリの**関連部分のみ**をコピーした snapshot。
最新・完全版は各 upstream を参照のこと。

| repo | branch | commit | 取得日 | 取り込み範囲 | 除外 |
|---|---|---|---|---|---|
| GreenTea_NetworkLatencyViewer | master | `bac5076` | 2026-06-26 | `docs/`・`tools/`・`testplan.md`・`README.md` | `results/`、`CLAUDE.md`/`HANDOFF.md`（内部メモ）、`tools/dashboard/static/components/`（3rd-party JS） |
| robot_comm_spec | v2.1.0-dev | `adbd58b` | 2026-06-25 | 全 `*.md` | — |
| SanRei_HID | dev | `1132740` | 2026-06-25 | `src/`・`docs/`・`README.md`・`*_tool.py` | `libraries/`・`utils/`（uf2/hidapi 等 3rd-party）、`arduino-cli` バイナリ、`test/` |

- upstream: [GreenTea_NetworkLatencyViewer](https://github.com/gochiuma/GreenTea_NetworkLatencyViewer) ／ [robot_comm_spec](https://github.com/greentea-ssl/robot_comm_spec) ／ [SanRei_HID](https://github.com/greentea-ssl/SanRei_HID)
- 注: 各 vendored ファイル内の相対リンクのうち、上記**除外ディレクトリを指すもの**は本コピー内では解決しない場合がある（その場合は upstream を参照）。
