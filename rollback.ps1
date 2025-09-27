# rollback.ps1
# GitHub Actions の workflow_dispatch を叩いて「Rollback ECS service」を起動するスクリプト
# 使い方:
#   .\rollback.ps1                            # ← 直前の1つ前リビジョンに戻す
#   .\rollback.ps1 -Revision 58               # ← 指定リビジョンに戻す
# 必要:
#   - 環境変数 GH_PAT に GitHub Personal Access Token（repo + workflow）が入っていること

param(
  [int]$Revision = -1  # 指定が無ければ -1（＝直前リビジョンへ）
)

$ErrorActionPreference = "Stop"

# === 設定（あなたの環境に合わせ済み） ===
$Owner  = "KW781kazu"         # GitHub ユーザー/Org
$Repo   = "voicebot"          # リポジトリ名
$WfFile = "rollback.yml"      # .github/workflows/ のファイル名
$ApiBase = "https://api.github.com"

# トークン確認
$Token = $env:GH_PAT
if ([string]::IsNullOrWhiteSpace($Token)) {
  Write-Error "環境変数 GH_PAT が設定されていません。PAT を作成して `\$env:GH_PAT` に入れてください。"
}

# workflow_dispatch の URL
$Url = "$ApiBase/repos/$Owner/$Repo/actions/workflows/$WfFile/dispatches"

# ボディ組み立て（target_revision は空なら未指定として扱う）
$inputs = @{}
if ($Revision -ge 0) {
  $inputs.target_revision = "$Revision"
}
$Body = @{
  ref = "main"
  inputs = $inputs
} | ConvertTo-Json

Write-Host "POST $Url" -ForegroundColor Cyan
Write-Host "Body: $Body"

# API 呼び出し（204 No Content が正）
$Headers = @{
  "Accept" = "application/vnd.github+json"
  "Authorization" = "Bearer $Token"
  "X-GitHub-Api-Version" = "2022-11-28"
}
Invoke-RestMethod -Method Post -Uri $Url -Headers $Headers -Body $Body

Write-Host "✅ Rollback workflow を起動しました。Actions タブで進行状況を確認してください。" -ForegroundColor Green
