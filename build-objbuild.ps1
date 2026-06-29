# WASM-PORT: OBJ -> .dig interior compiler harness. Node-runnable (no GL).
# Links the engine objects + the (newly ported) BSP build files itrbsp/itrportal/
# itr3dmimport with test\objbuild.cpp. Uses NODERAWFS so argv file paths work.
#   node build\objbuild.js <in.obj> <out.dig>
param([switch]$Clean)

$root = $PSScriptRoot
& "$root\tools\emsdk\emsdk_env.ps1" 2>$null | Out-Null
$obj   = "$root\build\obj"
$objs2 = "$root\build\obj-objbuild"
if ($Clean -and (Test-Path $objs2)) { Remove-Item -Recurse -Force $objs2 }
New-Item -ItemType Directory -Force $objs2 | Out-Null

if (-not (Test-Path "$obj\itrgeometry.o")) {
  Write-Host "build\obj looks empty - run build.ps1 first."; exit 1
}

$inc = @(
  "$root\shim", "$root\engine\inc", "$root\engine\Core\inc", "$root\engine\Ml\Inc",
  "$root\engine\Dgfx\inc", "$root\engine\DNet\inc", "$root\engine\Sim\inc",
  "$root\engine\SimGui\inc", "$root\engine\SimObjects\Inc", "$root\engine\Ts3\Inc",
  "$root\engine\Terrain\Inc", "$root\engine\Terrain2\inc", "$root\engine\Interior\inc",
  "$root\engine\Landscape\Inc", "$root\engine\Window\Inc", "$root\engine\console\inc",
  "$root\engine\common\Sim\Inc", "$root\engine\Main\Inc", "$root\engine\DirectX\inc"
) | ForEach-Object { "-I$_" }

$flags = @('-std=c++14','-fms-extensions','-fpermissive','-Wno-everything',
           '-DWIN32','-D_WIN32','-DNDEBUG','-DDLLAPI=','-fwasm-exceptions',
           '-fno-operator-names','-O1')

# the 3 BSP build files (not part of the runtime manifest) + the harness
$srcs = @{
  "itrbsp"        = "$root\engine\Interior\code\itrbsp.cpp"
  "itrportal"     = "$root\engine\Interior\code\itrportal.cpp"
  "itr3dmimport"  = "$root\engine\Interior\code\itr3dmimport.cpp"
  "itrbasiclighting" = "$root\engine\Interior\code\itrbasiclighting.cpp"
  "itrmatrix"     = "$root\engine\Interior\code\itrmatrix.cpp"   # itrInverse
  "zedPersLight"  = "$root\engine\Interior\code\zedPersLight.cpp"
  "tpoly"         = "$root\engine\Interior\code\tpoly.cpp"   # TPolyVertex ctor
  "wasm_net"      = "$root\shim\wasm_net.cpp"                # net symbols UDPNet.o needs (unused)
  "objbuild"      = "$root\test\objbuild.cpp"
}
foreach ($name in $srcs.Keys) {
  $out = emcc -c $srcs[$name] -o "$objs2\$name.o" @inc @flags 2>&1
  if ($LASTEXITCODE -ne 0) {
    $out | Select-Object -First 25 | ForEach-Object { Write-Host $_ }
    Write-Host "FAIL: $name"; exit 1
  }
}

# all engine objects except harness mains
$engineObjs = Get-ChildItem "$obj\*.o" |
  Where-Object { $_.Name -ne 'smoke.o' } |
  ForEach-Object { $_.FullName }
$newObjs = Get-ChildItem "$objs2\*.o" | ForEach-Object { $_.FullName }

emcc @engineObjs @newObjs -o "$root\build\objbuild.js" `
  -fwasm-exceptions -sALLOW_MEMORY_GROWTH=1 -sLEGACY_GL_EMULATION=1 `
  -sNODERAWFS=1 -sEXIT_RUNTIME=1 -sSTACK_SIZE=8MB -O1 `
  2>&1 | Select-Object -First 50
if ($LASTEXITCODE -eq 0) { Write-Host "LINKED: build\objbuild.js" } else { exit 1 }
