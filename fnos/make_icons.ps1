Add-Type -AssemblyName System.Drawing

function RoundedPath([single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r * 2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

function New-BaseIcon {
    $bmp = New-Object System.Drawing.Bitmap(256, 256)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    # 背景：圆角 + 蓝绿渐变
    $bgPath = RoundedPath 8 8 240 240 54
    $c1 = [System.Drawing.ColorTranslator]::FromHtml('#2563EB')
    $c2 = [System.Drawing.ColorTranslator]::FromHtml('#0EA5A5')
    $rect = New-Object System.Drawing.RectangleF(8, 8, 240, 240)
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $c1, $c2, 55)
    $g.FillPath($brush, $bgPath)
    $brush.Dispose()

    # 车身
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $bodyPath = RoundedPath 40 124 176 62 24
    $g.FillPath($white, $bodyPath)

    # 车顶
    $roof = New-Object System.Drawing.Drawing2D.GraphicsPath
    $roof.AddPolygon(@(
        (New-Object System.Drawing.PointF(84, 128)),
        (New-Object System.Drawing.PointF(104, 92)),
        (New-Object System.Drawing.PointF(154, 92)),
        (New-Object System.Drawing.PointF(180, 128))
    ))
    $g.FillPath($white, $roof)

    # 车窗
    $glass = New-Object System.Drawing.SolidBrush([System.Drawing.ColorTranslator]::FromHtml('#BFD7FA'))
    $win = New-Object System.Drawing.Drawing2D.GraphicsPath
    $win.AddPolygon(@(
        (New-Object System.Drawing.PointF(101, 120)),
        (New-Object System.Drawing.PointF(113, 102)),
        (New-Object System.Drawing.PointF(145, 102)),
        (New-Object System.Drawing.PointF(161, 120))
    ))
    $g.FillPath($glass, $win)
    $win.Dispose()

    # 车轮
    $tire = New-Object System.Drawing.SolidBrush([System.Drawing.ColorTranslator]::FromHtml('#111827'))
    $hub  = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $g.FillEllipse($tire, 70, 164, 40, 40)
    $g.FillEllipse($tire, 146, 164, 40, 40)
    $g.FillEllipse($hub, 83, 177, 14, 14)
    $g.FillEllipse($hub, 159, 177, 14, 14)

    # 通知角标
    $red = New-Object System.Drawing.SolidBrush([System.Drawing.ColorTranslator]::FromHtml('#EF4444'))
    $g.FillEllipse($red, 156, 24, 74, 74)
    $g.FillRectangle($white, 188, 40, 11, 26)
    $g.FillEllipse($white, 188, 72, 11, 11)

    $red.Dispose(); $tire.Dispose(); $hub.Dispose(); $glass.Dispose(); $white.Dispose()
    $roof.Dispose(); $bodyPath.Dispose(); $bgPath.Dispose()
    $g.Dispose()
    return $bmp
}

function Save-Icon([System.Drawing.Bitmap]$base, [int]$size, [string]$path) {
    if ($size -eq 256) {
        $base.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
        return
    }
    $out = New-Object System.Drawing.Bitmap($size, $size)
    $g = [System.Drawing.Graphics]::FromImage($out)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($base, 0, 0, $size, $size)
    $g.Dispose()
    $out.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $out.Dispose()
}

$root = $PSScriptRoot
$base = New-BaseIcon

Save-Icon $base 256 (Join-Path $root 'ICON_256.PNG')
Save-Icon $base 64  (Join-Path $root 'ICON.PNG')

$imgDir = Join-Path $root 'app/ui/images'
if (-not (Test-Path $imgDir)) { New-Item -ItemType Directory -Path $imgDir -Force | Out-Null }
Save-Icon $base 256 (Join-Path $imgDir 'icon_256.png')
Save-Icon $base 64  (Join-Path $imgDir 'icon_64.png')
$base.Dispose()

Get-ChildItem (Join-Path $root 'ICON*.PNG') | ForEach-Object { "$($_.Name) $($_.Length) bytes" }
