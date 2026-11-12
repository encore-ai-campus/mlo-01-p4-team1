#requires -Version 7.4
param([ValidateRange(1024, 65535)][int]$Port = 8765)

$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot
$localRoot = Split-Path -Parent (Split-Path -Parent $app)
$python = Join-Path $localRoot 'song-requests-test/Scripts/python.exe'
$runDir = Join-Path $app '.local-preview-logs/luna-local'
$url = "http://127.0.0.1:$Port/playlist"
$db = Join-Path $runDir 'playlist.sqlite3'
$config = @{
    C_LOCAL_DEMO = '1'
    C_LOCAL_DATABASE = $db
    FRONTEND_REQUIRED = '1'
    INTEGRATION_ENABLED = '0'
    HOURLY_RECOMMENDATION_ENABLED = '1'
    WEATHER_PROVIDER = ''
    GEMINI_ENABLED = '0'
    PHASE4_TRUSTED_PROXY_CIDRS = ''
    DJANGO_ALLOWED_HOSTS = '127.0.0.1,localhost'
    YOUTUBE_REGISTRY_BACKEND = 'local'
    OPENAI_API_KEY = ''
    OPENAI_PLAYLIST_ORDER_MODEL = 'gpt-6-luna'
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
function Show-KeyDialog {
    $form = [Windows.Forms.Form]::new()
    try {
        $form.Text = '뮤더 로컬 테스트 · OpenAI API 키'
        $form.ClientSize = [Drawing.Size]::new(468, 152)
        $form.StartPosition = 'CenterScreen'
        $form.FormBorderStyle = 'FixedDialog'
        $form.MaximizeBox = $false
        $form.MinimizeBox = $false
        $form.TopMost = $true
        $form.Font = [Drawing.Font]::new('Malgun Gothic', 9)

        $label = [Windows.Forms.Label]::new()
        $label.Location = [Drawing.Point]::new(16, 14)
        $label.Size = [Drawing.Size]::new(436, 42)
        $label.Text = "GPT-6 Luna 테스트에 사용할 OpenAI API 키를 입력해 주세요.`n키는 파일에 저장하지 않습니다."
        $box = [Windows.Forms.TextBox]::new()
        $box.SetBounds(16, 61, 338, 26)
        $box.UseSystemPasswordChar = $true
        $pasteKey = {
            try {
                if ([Windows.Forms.Clipboard]::ContainsText()) {
                    $box.Text = [Windows.Forms.Clipboard]::GetText().Trim()
                    $box.SelectionStart = $box.TextLength
                    [void]$box.Focus()
                }
            } catch {
                [void][Windows.Forms.MessageBox]::Show($form, '클립보드를 읽지 못했습니다. 다시 복사한 뒤 붙여넣어 주세요.', '붙여넣기')
            }
        }.GetNewClosure()
        $paste = [Windows.Forms.Button]::new()
        $paste.SetBounds(364, 60, 88, 28)
        $paste.Text = '붙여넣기'
        $paste.Add_Click($pasteKey)
        $box.Add_KeyDown({
            param($sender, $eventArgs)
            if ($eventArgs.Control -and $eventArgs.KeyCode -eq 'V') {
                $eventArgs.SuppressKeyPress = $true
                & $pasteKey
            }
        }.GetNewClosure())

        $ok = [Windows.Forms.Button]::new()
        $ok.SetBounds(272, 105, 84, 30)
        $ok.Text = '시작'
        $ok.DialogResult = [Windows.Forms.DialogResult]::OK
        $ok.Enabled = $false
        $box.Add_TextChanged({ $ok.Enabled = -not [string]::IsNullOrWhiteSpace($box.Text) }.GetNewClosure())
        $cancel = [Windows.Forms.Button]::new()
        $cancel.SetBounds(368, 105, 84, 30)
        $cancel.Text = '취소'
        $cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
        $form.Controls.AddRange(@($label, $box, $paste, $ok, $cancel))
        $form.AcceptButton = $ok
        $form.CancelButton = $cancel
        $form.ActiveControl = $box
        $form.Add_Shown({ Set-Content -LiteralPath (Join-Path $runDir 'status.txt') -Value 'awaiting_key' }.GetNewClosure())
        if ($form.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
            return $box.Text.Trim()
        }
    } finally {
        if ($box) { $box.Clear() }
        $form.Dispose()
    }
}

New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$status = Join-Path $runDir 'status.txt'
$web = $null
$worker = $null
try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $db -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $app 'frontend/dist/index.html') -PathType Leaf)) {
        throw 'Local test dependencies are not prepared.'
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start() } finally { $listener.Stop() }
    $key = Show-KeyDialog
    if ([string]::IsNullOrWhiteSpace($key)) {
        Set-Content -LiteralPath $status -Value 'cancelled'
        return
    }
    $base = @{
        FilePath = $python
        WorkingDirectory = $app
        WindowStyle = 'Hidden'
        PassThru = $true
    }
    Set-Content -LiteralPath $status -Value 'starting'
    $web = Start-Process @base -Environment $config `
        -ArgumentList @('-X','utf8','-B','backend/manage.py','runserver',"127.0.0.1:$Port",'--noreload') `
        -RedirectStandardOutput (Join-Path $runDir 'web.out.log') -RedirectStandardError (Join-Path $runDir 'web.err.log')
    try {
        $config['OPENAI_API_KEY'] = $key
        $worker = Start-Process @base -Environment $config `
            -ArgumentList @('-X','utf8','-B','backend/manage.py','run_recommendations','--queue','rule') `
            -RedirectStandardOutput (Join-Path $runDir 'worker.out.log') -RedirectStandardError (Join-Path $runDir 'worker.err.log')
    } finally {
        $config['OPENAI_API_KEY'] = ''
        $key = $null
    }
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        if ($web.HasExited -or $worker.HasExited) { break }
        try {
            $result = Invoke-WebRequest -Uri $url -TimeoutSec 1
            if ($result.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'The local web server or worker did not start.' }
    @{ url = $url; web_pid = $web.Id; worker_pid = $worker.Id } |
        ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $runDir 'processes.json')
    Set-Content -LiteralPath $status -Value 'ready'
    Wait-Process -Id $web.Id, $worker.Id -Any
} catch {
    foreach ($process in @($worker, $web)) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
    Set-Content -LiteralPath $status -Value 'failed'
    [void][Windows.Forms.MessageBox]::Show(
        '로컬 테스트를 시작하지 못했습니다. Codex에서 상태를 확인해 주세요.', '뮤더 로컬 테스트')
    exit 1
} finally {
    $key = $null
    $config['OPENAI_API_KEY'] = ''
}
