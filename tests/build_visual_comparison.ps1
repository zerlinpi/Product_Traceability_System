param(
    [string]$PreviousComparison = "tests\ui-design-comparison.png",
    [string]$CurrentScreenshot = "tests\ui-dashboard-final.png",
    [string]$Output = "tests\ui-design-comparison-final.png"
)

Add-Type -AssemblyName System.Drawing

$previousPath = (Resolve-Path -LiteralPath $PreviousComparison).Path
$currentPath = (Resolve-Path -LiteralPath $CurrentScreenshot).Path
$previous = [System.Drawing.Image]::FromFile($previousPath)
$current = [System.Drawing.Image]::FromFile($currentPath)
$slotWidth = [int]($previous.Width / 2)
$slotHeight = $previous.Height
$headerHeight = 34
$canvas = [System.Drawing.Bitmap]::new($slotWidth * 2, $slotHeight + $headerHeight)
$graphics = [System.Drawing.Graphics]::FromImage($canvas)
$graphics.Clear([System.Drawing.Color]::White)
$graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic

$font = [System.Drawing.Font]::new("Microsoft YaHei UI", 12, [System.Drawing.FontStyle]::Bold)
$brush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(48, 58, 70))
$graphics.DrawString("Reference", $font, $brush, 12, 7)
$graphics.DrawString("Current build (1488 x 1058)", $font, $brush, $slotWidth + 12, 7)

$leftDestination = [System.Drawing.Rectangle]::new(0, $headerHeight, $slotWidth, $slotHeight)
$leftSource = [System.Drawing.Rectangle]::new(0, 0, $slotWidth, $slotHeight)
$rightDestination = [System.Drawing.Rectangle]::new($slotWidth, $headerHeight, $slotWidth, $slotHeight)
$rightSource = [System.Drawing.Rectangle]::new(0, 0, $current.Width, $current.Height)
$graphics.DrawImage($previous, $leftDestination, $leftSource, [System.Drawing.GraphicsUnit]::Pixel)
$graphics.DrawImage($current, $rightDestination, $rightSource, [System.Drawing.GraphicsUnit]::Pixel)

$canvas.Save((Join-Path (Get-Location) $Output), [System.Drawing.Imaging.ImageFormat]::Png)

$brush.Dispose()
$font.Dispose()
$graphics.Dispose()
$canvas.Dispose()
$current.Dispose()
$previous.Dispose()
