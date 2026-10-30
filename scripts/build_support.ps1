# 构建只需一个原生命令边界：每一步失败立即停止，不能让后续退出码覆盖错误。
function Invoke-BuildCommand {
  param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string[]]$ArgumentList = @()
  )

  & $FilePath @ArgumentList
  if ($LASTEXITCODE -ne 0) {
    throw "$Label 失败（退出码 $LASTEXITCODE）"
  }
}

# 删除前验证绝对路径及沿途链接，避免构建目录变量意外指向工作区之外。
function Assert-BuildDirectory {
  param([string]$Path, [string]$WorkspaceRoot)
  $rootFull = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
  $targetFull = [System.IO.Path]::GetFullPath($Path)
  $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
  if (-not $targetFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "构建目录越界：$targetFull"
  }
  $current = $targetFull
  while ($current -and $current.Length -ge $rootFull.Length) {
    $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      throw "构建目录不允许经过符号链接或联接点：$current"
    }
    if ($current -eq $rootFull) { break }
    $current = [System.IO.Path]::GetDirectoryName($current)
  }
  return $targetFull
}

function Remove-BuildDirectory {
  param([string]$Path, [string]$WorkspaceRoot)
  $checked = Assert-BuildDirectory $Path $WorkspaceRoot
  if (Test-Path -LiteralPath $checked) { Remove-Item -LiteralPath $checked -Recurse -Force }
}
