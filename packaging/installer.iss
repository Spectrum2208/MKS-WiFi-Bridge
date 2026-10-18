; Установщик MKS WiFi Bridge (Inno Setup 6). Собирается из packaging\build_exe.py после MKSBridge.exe.
; Ставится в папку пользователя, права администратора не нужны.

#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{5B627000-2909-4F0D-8103-A8EF88D2C04B}
AppName=MKS WiFi Bridge
AppVersion={#AppVersion}
AppVerName=MKS WiFi Bridge {#AppVersion}
AppPublisher=Spectrum2208
AppPublisherURL=https://github.com/Spectrum2208/MKS-WiFi-Bridge
AppSupportURL=https://github.com/Spectrum2208/MKS-WiFi-Bridge/issues
DefaultDirName={autopf}\MKS WiFi Bridge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
WizardStyle=modern
; язык мастера — по языку Windows, без лишнего окна выбора (сама панель пока только на русском)
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\MKSBridge.exe
UninstallDisplayName=MKS WiFi Bridge
OutputDir=..\dist
OutputBaseFilename=MKSBridge-Setup
Compression=lzma2
SolidCompression=yes
; мост работает в фоне: при обновлении и удалении его надо закрыть
CloseApplications=force
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
russian.AutoStart=Запускать вместе с Windows (в фоне, значок в трее)
english.AutoStart=Start with Windows (in the background, tray icon)
russian.Launch=Запустить MKS WiFi Bridge
english.Launch=Launch MKS WiFi Bridge

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutoStart}"

[Files]
Source: "..\dist\MKSBridge.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\MKS WiFi Bridge"; Filename: "{app}\MKSBridge.exe"
Name: "{autodesktop}\MKS WiFi Bridge"; Filename: "{app}\MKSBridge.exe"; Tasks: desktopicon

[Registry]
; та же запись, что пишет переключатель «Запускать вместе с Windows» в панели (mks_bridge\autostart.py)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MKSBridge"; ValueData: """{app}\MKSBridge.exe"" --no-browser"; Tasks: autostart
; удалить автозапуск при удалении программы, даже если его включили потом из панели
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "MKSBridge"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\MKSBridge.exe"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM MKSBridge.exe"; Flags: runhidden; RunOnceId: "StopBridge"
