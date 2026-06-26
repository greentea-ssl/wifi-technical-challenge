# Versioning Policy

このリポジトリ自身、および本仕様をサブモジュールとして取り込む各ファームウェアのバージョン採番規則を定義する。

## 1. robot_comm_spec のバージョン

[Semantic Versioning](https://semver.org/) (`vMAJOR.MINOR.PATCH`) に従う。

| レベル | 意味 | 該当する変更 |
|---|---|---|
| **MAJOR** | プロトコル破壊変更 | バイト位置の変更、ID 削除、互換性のないセマンティクス変更 |
| **MINOR** | 後方互換な追加 | 新メッセージ、新ビット定義、新エンドポイント、reserved 領域の意味付け |
| **PATCH** | 意味論的差分なし | 説明・図・例の追加、誤字修正、表記統一 |

リリースは `master` 上で annotated タグ (`vMAJOR.MINOR.PATCH`) として作成し、対応する GitHub Release を発行する。

## 2. コンシューマーファームウェアのバージョン

本仕様をサブモジュールとして取り込むファームウェアリポジトリ (PowerBoard, SanRei_HID, greentea_main_firmware など) は、自身のバージョンを次の形式で採番する。

### 形式

```
<branch>_v<spec_major>.<spec_minor>.<firmware_patch>
```

| 構成要素 | 意味 |
|---|---|
| `<branch>` | リリース元ブランチ名 (`master`, `dev` など) |
| `<spec_major>` | サブモジュールが指している `robot_comm_spec` の MAJOR |
| `<spec_minor>` | 同 MINOR |
| `<firmware_patch>` | この `<spec_major>.<spec_minor>` 配下でのファーム独自パッチ番号 (0 始まり) |

> **spec の PATCH 番号はファームウェアバージョンには反映されない。**
> spec の PATCH 更新は意味論的差分が無いため、ファームをバンプする理由にはならない。

### 例

| ファームの状況 | バージョン |
|---|---|
| spec `v1.0.0` 対応・`dev` ブランチ・初リリース | `dev_v1.0.0` |
| 上記の次のリリース (ファームのみ更新) | `dev_v1.0.1` |
| spec `v1.0.5` (説明追記の累積) に追従後の最初のリリース | `dev_v1.0.2` (PATCH は反映しないため連番継続) |
| spec `v1.1.0` (新メッセージ追加) に追従後の最初のリリース | `dev_v1.1.0` (パッチリセット) |
| spec `v1.2.3` 対応・`dev` ブランチで `firmware_patch=2` | `dev_v1.2.2` |
| spec `v2.0.0` (破壊変更) 対応・`master` 初リリース | `master_v2.0.0` |

### 運用ルール

- spec の **MAJOR または MINOR** が変わったら、ファームのパッチ番号は **0 にリセット** する。
- spec の **PATCH** が変わってもファームをバンプする必要はない (上げても良いが意味は薄い)。
- ブランチごとに独立したパッチ系列を持つため、`master` と `dev` で同じバージョン文字列が衝突することは無い。
- ファーム側 release ワークフローはタグ push をトリガとし、タグ名をそのままバージョン文字列としてバイナリへ埋め込む運用を想定。
