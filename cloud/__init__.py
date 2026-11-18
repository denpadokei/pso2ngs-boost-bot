"""共有Bot（Azure Functions 側）の実装。

- ``config``    … 環境変数からなる設定
- ``storage``   … Table Storage（guilds / state）へのアクセス
- ``runner``    … 週次バッチ（Timer から呼ばれる）
- ``interactions`` … Discord HTTP Interactions（/setup）※Phase 2
- ``commands``  … スラッシュコマンドの登録スクリプト ※Phase 2
"""
