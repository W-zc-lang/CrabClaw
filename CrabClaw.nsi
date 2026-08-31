; CrabClaw.nsi — NSIS 安装脚本
; 打包命令（使用临时下载的 makensis.exe）：
;   makensis.exe CrabClaw.nsi

!define APP_NAME "CrabClaw"
!define APP_VERSION "1.0.8"
!define APP_PUBLISHER "W-zc-lang（则成吴）"
!define APP_WEB_SITE "https://github.com/W-zc-lang"
!define APP_EXE "CrabClaw.exe"
!define APP_ICO "icon.ico"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "dist\CrabClaw_Setup.exe"
InstallDir "$LOCALAPPDATA\${APP_NAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
AllowRootDirInstall false

; 界面
!define MUI_ABORTWARNING
!define MUI_ICON "${APP_ICO}"
!define MUI_UNICON "${APP_ICO}"

!include "MUI2.nsh"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

Section "CrabClaw" SecMain
  SetOutPath "$INSTDIR"
  File "dist\CrabClaw.exe"
  File "icon.ico"
  File "AUTHORS.txt"

  ; 卸载程序
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 开始菜单快捷方式
  CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_ICO}" 0
  ; 桌面快捷方式
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_ICO}" 0

  ; 写入注册表，便于后续卸载/升级定位
  WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APP_NAME}" "Version" "${APP_VERSION}"
SectionEnd

Section "Uninstall"
  ; 删除安装文件
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\${APP_ICO}"
  Delete "$INSTDIR\AUTHORS.txt"
  Delete "$INSTDIR\uninstall.exe"

  ; 删除快捷方式
  Delete "$SMPROGRAMS\${APP_NAME}.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  ; 删除注册表
  DeleteRegKey HKCU "Software\${APP_NAME}"

  ; 删除目录（仅空目录）
  RMDir "$INSTDIR"
SectionEnd
