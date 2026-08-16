<#
  install-tools.ps1 - install the subtitle toolchain (ffsubsync + a usable VAD).

  Usage:  pwsh -File install-tools.ps1 [-Vad silero|auditok] [-SkipModel]

  WHY this is not just "pip install ffsubsync": on current Python (3.13+) the
  straightforward install fails in four separate ways, each with a non-obvious
  fix. This script encodes the working combination.

    1. ffsubsync depends on webrtcvad-wheels, which has no wheel for 3.13/3.14
       and will not build without MSVC. Install with --no-deps and supply the
       rest by hand.
    2. ffsubsync pins auditok<0.2.0. auditok 0.2.0 changed AudioEnergyValidator
       (it gained a required `channels` arg) and 0.3+ removed ADSFactory
       entirely, so only 0.1.5 works.
    3. auditok 0.1.5 imports `audioop`, removed from the stdlib in 3.13.
       audioop-lts is the maintained backport.
    4. auditok is the WEAKEST detector ffsubsync offers, and with it roughly
       half of all alignments fail - returning a value pinned to the edge of
       the +/-60s search range rather than a real measurement. silero (neural,
       needs torch) is strongly preferred. torch.hub also refuses to fetch the
       model non-interactively unless the repo is pre-trusted, which is what
       the model step below does.

  ffmpeg must already be on PATH (ffsubsync shells out to it).
#>
param(
  [ValidateSet('silero', 'auditok')][string]$Vad = 'silero',
  [switch]$SkipModel
)

$ErrorActionPreference = 'Stop'

function Pip { param([string[]]$PipArgs) & python -m pip install --quiet @PipArgs; if ($LASTEXITCODE) { throw "pip install failed: $($PipArgs -join ' ')" } }

Write-Host '=== python / ffmpeg ===' -ForegroundColor Cyan
& python --version
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) { throw 'ffmpeg not found on PATH - install it first (ffsubsync shells out to it).' }
& ffmpeg -version | Select-Object -First 1

Write-Host "`n=== ffsubsync (no deps: webrtcvad-wheels has no modern wheel) ===" -ForegroundColor Cyan
Pip @('--no-deps', 'ffsubsync')

Write-Host "`n=== ffsubsync's real dependencies ===" -ForegroundColor Cyan
Pip @('numpy', 'scipy', 'pysubs2', 'srt', 'ffmpeg-python', 'tqdm', 'rich',
      'charset_normalizer', 'chardet', 'six', 'packaging', 'requests')

Write-Host "`n=== auditok 0.1.5 + audioop backport ===" -ForegroundColor Cyan
# 0.1.5 exactly: 0.2.0 changed AudioEnergyValidator, 0.3+ dropped ADSFactory.
Pip @('auditok==0.1.5')
# audioop left the stdlib in 3.13; auditok 0.1.5 still imports it.
if ([version]"$($PSVersionTable.PSVersion)" -and $true) { Pip @('audioop-lts') }

if ($Vad -eq 'silero') {
  Write-Host "`n=== silero VAD (torch CPU) ===" -ForegroundColor Cyan
  Pip @('torch', 'torchaudio', '--index-url', 'https://download.pytorch.org/whl/cpu')
  if (-not $SkipModel) {
    Write-Host 'Fetching + trusting the silero model (torch.hub prompts interactively otherwise)...'
    $py = @'
import torch
m, _ = torch.hub.load(repo_or_dir="snakers4/silero-vad", model="silero_vad",
                      trust_repo=True, onnx=False)
print("silero ok:", type(m).__name__)
'@
    & python -c $py
    if ($LASTEXITCODE) { throw 'silero model fetch failed' }
  }
}

Write-Host "`n=== verify ===" -ForegroundColor Cyan
$verify = @'
import sys
import ffsubsync
print("ffsubsync", ffsubsync.__version__)
from auditok import ADSFactory, AudioEnergyValidator
print("auditok ok")
try:
    import torch; print("torch", torch.__version__)
except ImportError:
    print("torch absent (auditok-only install)")
'@
& python -c $verify
if ($LASTEXITCODE) { throw 'verification failed' }

Write-Host "`nNOTE: ffsubsync installs no console script here; invoke it as:" -ForegroundColor Yellow
Write-Host '  python -c "import sys; from ffsubsync import main; sys.argv=[''ffsubsync'']+sys.argv[1:]; sys.exit(main())" ...'
Write-Host "Capture its output with encoding='utf-8' - its rich output breaks cp1252 decoding on Windows." -ForegroundColor Yellow
