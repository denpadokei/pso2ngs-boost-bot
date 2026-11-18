# PSO2NGS ブーストキャンペーン Discord 通知

『PSO2 ニュージェネシス』の**ブーストキャンペーン**（レアドロップ倍率アップなど）を検知して、
Discord のチャンネルへ自動投稿する小さなツールです。
DisCal のような「イベント告知 + カレンダー運用」を、このツールだけで完結できます。

- 新しく告知されたブーストを通知
- 開始日・終了日（通常は水曜）の実行時にリマインド
- 対象クエスト、全プレイヤー／プレミアムの倍率差も表示
- Discord の**スケジュールイベント**（RSVP 付き）も自動作成・更新
- Gateway 接続なし。常駐サーバー不要（GitHub Actions の無料枠で運用できます）

## 仕組み

情報源は公式プレイヤーズサイトの3か所です。

| 取得元 | 用途 |
| --- | --- |
| `https://pso2.jp/players/pso2news.xml` | 新着記事の検知（RSS 1.0 / RDF） |
| `https://pso2.jp/players/schedule/` | ブーストキャンペーンの一覧と開催期間（リマインドの基準） |
| `https://pso2.jp/players/news/maintenance/` | メンテナンス予定表（「メンテナンス開始／終了」の実時刻） |
| 告知記事の本文（例: `/players/news/32486/`） | 対象クエスト、レアドロップ倍率などの詳細 |

メンテナンス告知には「12月2日（水）8:00 ～ 17:00」のような**未来の予定**が載るので、
これを読み取って期間の「メンテナンス開始／終了」を実時刻に置き換えます。
取得できない場合だけ 11:00 を仮置きします。

RSS で新しい告知記事を拾い、本文に「倍率アップ」「成功率アップ」「○%オフ」のような
効果の表があればイベント化します。見出しが「ブーストキャンペーン」でなくても、
「装備強化応援キャンペーン」のような効果系キャンペーンを拾えるのが特徴です。
スケジュールページに「ブーストキャンペーン 第1弾」のように期間付きで掲載される場合は、
記事タイトルに「ブースト」がなくても検知できます。
同じイベントは「ラベル + 期間」から作るキーで同一視するので、RSS とスケジュールの
両方に出てきても二重通知にはなりません。

## クイックスタート

### 1. 必要なもの

- Python 3.10 以上
- Discord サーバーの「チャンネルを管理」権限（Webhook を使う場合）
- Bot を作る場合は「イベントの作成」「イベントの管理」権限

### 2. 投稿先を用意する

**A. Webhook（いちばん簡単）**

Discord で通知したいチャンネルを右クリック →「チャンネルの編集」→「連携サービス」
→「ウェブフックを作成」→ 名前を決めて「ウェブフック URL をコピー」。

**B. Bot（イベントも作るならこちら）**

後述の「Discord のイベント（RSVP 付き）を作る」で Bot を作り、
`DISCORD_BOT_TOKEN` と `DISCORD_CHANNEL_ID` を設定すれば、Webhook なしで投稿できます。
両方設定した場合は Webhook が優先されます。

### 3. ローカルで動かす

```bash
cd pso2ngs-boost-bot
python -m pip install -r requirements.txt
copy .env.example .env        # macOS / Linux は cp
```

`.env` に URL を書きます。

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/....
```

動作確認：

```bash
# 現在の予定を表示
python -m pso2boost list

# 通知内容を確認（投稿しない）
python -m pso2boost check --dry-run

# Webhook の疎通確認
python -m pso2boost test-notify

# 本番（状態を保存して投稿）
python -m pso2boost check
```

`check` を cron などで週1回（水曜 13:00 JST）実行すれば運用できます。
サーバーが UTC の場合は `0 4 * * 3` を指定してください。

```cron
# 毎週水曜 13:00（サーバーが JST の場合）
0 13 * * 3 cd /path/to/pso2ngs-boost-bot && /usr/bin/python3 -m pso2boost check
```

### 4. GitHub Actions で無料運用する

1. このフォルダを GitHub リポジトリへ push
2. リポジトリの **Settings → Secrets and variables → Actions** に
   `DISCORD_WEBHOOK_URL` を登録
   （Bot で投稿・イベント作成する場合は `DISCORD_BOT_TOKEN`、`DISCORD_GUILD_ID`、
   必要なら `DISCORD_CHANNEL_ID` と `DISCORD_MENTION_ROLE_ID` も）
3. **Settings → Actions → General → Workflow permissions** を
   「Read and write permissions」に変更
4. `.github/workflows/boost.yml` が毎週水曜 13:00（JST）に実行されます（手動実行も可）

ワークフローは実行のたびに `data/state.json` をコミットして「通知済み」を覚えます。
このコミットは、GitHub の「60日間更新がないとスケジュール実行が止まる」仕様の回避も兼ねています。

## Discord のイベント（RSVP 付き）を作る

DisCal の置き換えとして、Discord 標準のスケジュールイベントも作成できます。
サーバーの「イベント」タブに表示され、メンバーは「興味あり」で RSVP できます。

### 1. Bot を作る

1. [Discord Developer Portal](https://discord.com/developers/applications) →「New Application」
2. 「General Information」の **Application ID** を控える（招待 URL の `client_id` に使います）
3. 左メニュー「Bot」→「Reset Token」→ トークンをコピー
4. 次の URL の `<APPLICATION_ID>` を置き換えて開き、サーバーを選んで認証

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot&permissions=17600775982080
```

権限を個別に選びたい場合は「OAuth2」→「URL Generator」で scope `bot` を選び、
**イベントの作成 / イベントの管理**（Bot で投稿するなら **メッセージを送信** と
**チャンネルを見る**）にチェックして生成します。

| 権限 | ビット | 用途 |
| --- | --- | --- |
| イベントの作成 (Create Events) | `1 << 44` | イベント作成。2026年2月以降は必須 |
| イベントの管理 (Manage Events) | `1 << 33` | イベントの更新・削除 |
| メッセージを送信 (Send Messages) | `1 << 11` | Bot で通知を投稿する場合 |
| チャンネルを見る (View Channels) | `1 << 10` | 同上 |

権限の合計は `17600775979008`（イベントのみ）、
`17600775982080`（+ 投稿）です。

補足：

- この Bot は Gateway に接続しないため、メンバー一覧では**オフライン表示のまま**です（正常）。
- Privileged Intents（Message Content / Server Members）は不要です。
- 招待直後に 403 が出る場合は、サーバー設定 → ロール で Bot の権限を確認してください。

### 2. 設定する

```dotenv
DISCORD_BOT_TOKEN=あなたのBotトークン
DISCORD_GUILD_ID=サーバーID
DISCORD_CHANNEL_ID=投稿先チャンネルID   # Bot で投稿する場合
```

サーバーID・チャンネルIDは、Discord の設定 →「詳細設定」→「開発者モード」を有効にして、
サーバー名／チャンネル名を右クリック →「IDをコピー」で取得できます。
Webhook で投稿する場合は `DISCORD_CHANNEL_ID` は不要です。

```bash
python -m pso2boost check --dry-run
```

で `[dry-run] イベント作成: ...` と出れば設定できています。

### 動き

- 未来に開始するキャンペーンだけイベントを作成（開始済みはスキップ）
- 名前・期間・場所・説明が変わったら自動で更新
- 終了から7日たったイベントは削除
- 通知メッセージには「イベント」リンク（`https://discord.com/events/...`）が付く

Discord 側の制限で、1サーバーにつき `SCHEDULED` / `ACTIVE` のイベントは最大100件です。
時刻が「メンテナンス開始」の場合はメンテナンス予定表の時刻を使います（取得できなければ 11:00 JST）。

## コマンド

```
python -m pso2boost check      # 新着・開始・終了を確認して通知
python -m pso2boost list       # 現在のブースト予定を表示（--json 可）
python -m pso2boost preview URL  # 告知記事の解析結果を表示（デバッグ用）
python -m pso2boost test-notify  # Webhook の疎通確認
```

共通オプション：

- `--dry-run` — 投稿せず内容を標準出力へ
- `--now 2026-11-25T09:30` — 基準日時を上書き（テスト用）
- `--delay 1.0` — リクエスト間隔（秒）
- `--state PATH` — 状態ファイル（既定 `data/state.json`）

## 通知イメージ

`NOTIFY_STYLE=discal`（既定）では、DisCal のイベント告知に寄せたカード形式で投稿します。

```
!~Event Announcement~!
Name: ブーストキャンペーン 第1弾
Description: ・レアドロップ倍率+100%
             ・プリセット能力付与倍率+50%
Start (Local): 2026年11月11日 水曜日 12:00
End (Local):   2026年11月18日 水曜日 11:00
Location: アークス双陣杯 / 星砕の暴進
Premium: ・レアドロップ倍率+50%
         ・プリセット能力付与倍率+50%
Event ID: 79c8704046d6
イベント: RSVP / イベントページ
告知記事: PSO2NGS 公式サイト
```

開始・終了リマインドでは Description の先頭に `【本日から】` / `【本日まで】` が付きます。
`NOTIFY_STYLE=compact` にすると、以前のシンプルな埋め込みに戻せます。

## 設定

| 環境変数 | 説明 |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Webhook で投稿する場合の URL |
| `DISCORD_BOT_TOKEN` | Discord イベントを作る場合の Bot トークン |
| `DISCORD_GUILD_ID` | Discord イベントを作る場合のサーバー ID |
| `DISCORD_CHANNEL_ID` | Webhook を使わず Bot で投稿する場合のチャンネル ID |
| `DISCORD_MENTION_ROLE_ID` | 新着通知でメンションするロール ID（任意） |
| `DISCORD_WEBHOOK_USERNAME` | 投稿名（任意。既定は Webhook の名前） |
| `DISCORD_WEBHOOK_AVATAR_URL` | アイコン画像 URL（任意） |
| `NOTIFY_STYLE` | `discal`（既定）または `compact` |
| `STATE_PATH` | 状態ファイルのパス（任意） |
| `BOOST_KEYWORDS` | 検知語を変えたい場合（カンマ区切り、既定 `ブースト,強化応援`） |

## リマインドの動き

実行は週1回（水曜 13:00 JST）を前提にしています。

- 新着：RSS で記事を検知、またはスケジュールに新しい行が現れた時点で投稿（同じ期間のイベントは一度だけ）
- 開始：開始日（通常は水曜）の実行時に「本日から」
- 終了：終了日（通常は水曜）の実行時に「本日まで」。
  終了時刻（11:00 など）を過ぎてから実行された場合は「終了しました」に切り替わります
- 告知と開始が同じ日なら、開始リマインドは省略
- RSS は直近 14 日分の記事だけを対象にします（初回実行時の過去記事掘りすぎ防止）

`メンテナンス開始` のような境界は、メンテナンス告知の予定表から実時刻を取得します。
取得できなかった場合のみ 11:00 JST を仮の境界として扱います。

## テスト

```bash
python -m pytest -q
```

`tests/fixtures/` に、公式ページから解析に必要な要素だけを抜き出した HTML / XML を置いてあり、
ネットワークなしで解析を検証できます（詳細は `tests/fixtures/README.md`）。

## 注意事項

- 非公式ツールです。SEGA とは関係ありません。
- ページ構造が変わると解析できなくなることがあります。その場合は `preview` で確認してください。
- 取得は週 1 回、リクエスト間隔 1 秒を既定にしています。間隔を詰めすぎないでください。
- 運営の告知内容・期間は変更されることがあります。最終的な判断は公式サイトをご確認ください。
- Discord イベントの開始・終了時刻は、メンテナンス予定表から取得します。
  取得できなかった場合のみ「メンテナンス開始」を 11:00 JST と仮定します。

## 公開リポジトリで使う場合

- Secrets（`DISCORD_BOT_TOKEN`、`DISCORD_WEBHOOK_URL` など）はリポジトリごとに設定してください。fork には引き継がれません。
- `.env` は `.gitignore` 済みです。コミットしないでください。
- `data/state.json` は実行状態です。公開情報と Discord のイベント ID だけを保存し、サーバー ID は含めません。テンプレートとして使う場合は削除して構いません。
- 定期実行は default ブランチのワークフローだけが動きます。fork して試す場合は手動実行（workflow_dispatch）を使ってください。

## ライセンス

[MIT License](LICENSE)

非公式ツールです。SEGA とは関係ありません。
`tests/fixtures/` のページ抜粋の権利は SEGA に帰属します。
