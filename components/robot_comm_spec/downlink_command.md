# UDP指令プロトコル

- リトルエンディアン

## JO2026仕様

ロボットへの送信は **mDNS unicast** で行う。

- ロボット ID は当該ロボットのトップマーカー番号と一致させる。
- HID は自身を `robot<robot_id>.local` という mDNS 名で広告するため、Host PC は当該名前解決によって得られた IP アドレスへ unicast 送信する。
- ポート番号は下記の各セクション (`AI指令用Packet`, `マニュアル操作用packet`, `全体連絡用`) を参照。

## AI指令用Packet(通常利用)
**unicast** (mDNS で `robot<robot_id>.local` を解決して送信)、Port:robot_id + 40000   
ex) id:0 = 40000, id:1 = 40001

> HID 側は自身を `robot<robot_id>.local` として mDNS 登録しているため、Host PC は当該名前解決によって得られた IP アドレスへ unicast 送信する。
> (旧仕様ではブロードキャスト送信であったが、現実装では unicast に移行済み。)

- 64bytes

```
# 00: 1111 1111 |HEADER_1 0xFF
# 01: 1100 0011 |HEADER_2 0xC3
# 02: 0000 xxxx |x:ID
# 03: aaaa aaaa |a:robot_x (mm, int16) Error : 0x7FFF
# 04:
# 05: bbbb bbbb |b:robot_y (mm, int16) Error : 0x7FFF
# 06:
# 07: cccc cccc |c:robot_theta (0-65535) Error : 0x7FFF
# 08:
# 09: tttt tttt |t:time stamp (mSec | current_pose_timestamp - _initialized_stamped))
# 10:
# 11: tttt tttt |t:time stamp (mSec | Packet transmission timing)
# 12:
# 13: uuuu uuuu |u:pos_cmd_x (mm, int16，ワールド座標)
# 14:
# 15: vvvv vvvv |v:pos_cmd_y (mm, int16)
# 16:
# 17: wwww wwww |w:pos_cmd_theta (0-65535)
# 18:
# 19: iiii iiii |i:pos_cmd_Vx (mm/s, int16)
# 20:
# 21: jjjj jjjj |j:pos_cmd_Vy (mm/s, int16)
# 22:
# 23: kkkk kkkk |k:pos_cmd_omega (rad/s, int16, Q10 fixed-point)
# 24:
# 25: iiii iiii |i:pos_cmd_ax (mm/s^2, int16)
# 26:
# 27: jjjj jjjj |j:pos_cmd_ay (mm/s^2, int16)
# 28:
# 29: kkkk kkkk |k:pos_cmd_aomega (rad/s^2, int16, Q10 fixed-point)
# 30:
# 31: iiii iiii |i:limit_velocity (mm/s, int16)
# 32:
# 33: iiii iiii |i:limit_omega (rad/s, int16, Q10 fixed-point)
# 34:
# 35: d01e fg10 |d:dribble_flag, e:kick_flag, f:chip_enable, g:auto_kick
# 36: gggg 0000 |g:dribble_power
# 37: pq00 0000 |p:esys_translate_control_enable, q: esys_rotate_control_enable
# 38-45:| unix time(8byte)
# 46: xxxx xxxx |x:esys_target_x (mm, int16, ロボット内座標, 機体中心原点)
# 47:
# 48: yyyy yyyy |y:esys_target_y (mm, int16, ロボット内座標, 機体中心原点)
# 49:
# 50: hhhh hhhh |h:kick_speed_cmd (m/s, uint8, Q5 fixed-point) (x 0.03125 m/s)
# 51: cccc cccc |c:cycle_count byte0 (LSB) (senderが走るごとに1加算)
# 52: cccc cccc |c:cycle_count byte1
# 53: cccc cccc |c:cycle_count byte2 (MSB, 24bit total)
# 54-61:        |dummy
# 62:           |checksum = XOR([2] ~ [61])
# 63:           |XOR(checksum,0xFF)
```
Freezeなどロボットに停止命令を送るときは`limit_velocity`, `limit_omega`を0にして送信する



## マニュアル操作用packet(テスト用、AI指令より優先される)

- ~~このpacketを受け取ったらロボットはマニュアル操作モードに切り替わる~~
- ~~5secでマニュアル操作モードがタイムアウト、通常動作モードに切り替わる(この後データが来なければ通常タイムアウト)~~
- ↑の仕様は廃止。
- ロボットがマニュアル操作モードで起動し、かつこのパケットが届いたときにマニュアル操作が可能
- SW1を押しながら起動するとマニュアル操作モードで起動する

- 64 bytes

```
    # 00: 1111 1111 |HEADER_1 0xFF
    # 01: 1100 1100 |HEADER_2 0xCC
    # 02: 0000 xxxx |x:ID
    # 03: iiii iiii |i:cmd_Vx (Robot based, mm/s, int16)
    # 04:
    # 05: jjjj jjjj |j:cmd_Vy (Robot based, mm/s, int16)
    # 06:
    # 07: kkkk kkkk |k:cmd_omega (rad/s, int16, Q10 fixed-point)
    # 08:
    # 09: d01e fg10 |d:dribble_flag, e:kick_flag, f:chip_enable, g:auto_kick
    # 10: gggg 0000 |g:dribble_power
    # 11: pq00 000r |p:esys_translate_control_enable, q: esys_rotate_control_enable. r: field_centric_mode
    # 12: xxxx xxxx |x:esys_target_x (mm, int16, ロボット内座標, 機体中心原点)
    # 13:
    # 14: yyyy yyyy |y:esys_target_y (mm, int16, ロボット内座標, 機体中心原点)
    # 15:
    # 16: hhhh hhhh |h:kick_speed_cmd (m/s, uint8, Q5 fixed-point) (x 0.03125 m/s)
    # 17-61:        |dummy
    # 62:           |checksum = XOR([2] ~ [61])
    # 63:           |XOR(checksum,0xFF)
```



## 全体連絡用 (EMS / OTA 共有ポート)

ブロードキャスト、Port: **40999**

このポートは EMS (緊急停止) と OTA 起動コマンドの **2 用途で共有** されている。
HID は受信した UDP パケットの先頭バイトで分岐する。

| 1 byte 目 | 用途 | 残りペイロード |
|---|---|---|
| `0x30` | OTA 起動 | OTA イメージの URL (NUL 終端 ASCII 文字列) |
| `0x30` 以外 | EMS heartbeat | ASCII テキスト (下記参照) |

### EMS heartbeat (緊急停止)

形式は **ASCII テキスト**。HID は受信ペイロード中に部分文字列 `stop` が含まれるかを `strstr` で判定する。
含まれていれば EMS heartbeat タイマを更新し、リモート EMO 状態 (`isRemoteEMO`) を ON にする。

```
例: "stop\n"
    "robot_stop_all"
    "{\"cmd\":\"stop\"}"     // いずれも "stop" を含むため有効
```

| 項目 | 値 |
|---|---|
| heartbeat タイムアウト | **3 秒** (`EMS_TIMEOUT_MS = 3000`) |
| タイムアウト時の挙動 | `isRemoteEMO` が自動で OFF に戻る (= release 相当) |
| robot ID 指定 | **無し** (全ロボット共通で受理。broadcast 運用前提) |



### EMS 発令時の挙動 (HID 内部)

- `isRemoteEMO` が ON になると、HID は CAN-LS バス経由で PowerBoard へリモート EMO 通知を発行する
- PowerBoard は `REMOTE_ESTOP` として処理する (詳細は [CAN_LS.md](./CAN_LS.md) および各ファームの仕様を参照)

### 運用上の注意

- AI 側からは特別なことがない限り発令しない。
- これが発令された状態でも、Vision の生データはロボットに送信する。
- Freeze の State でもこれを送らない。
- heartbeat 方式のため、停止を維持したい間は **3 秒以内の周期で `stop` を含むパケットを再送し続ける** 必要がある。


---

> **PC → HID 直結の汎用 CAN ブリッジ (Port 41000+id, JSON)** は本書ではなく [hid_bridge.md](./hid_bridge.md) を参照。CU を介さずパラメータ調整や CAN フレーム直接送出を行う診断用チャネル (v2.0.0 で導入)。