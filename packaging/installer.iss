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
; язык мастера — по языку Windows, без лишнего окна выбора
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
; тихая установка — это обновление из панели (mks_bridge\update.py): мост запускаем сами, панель уже открыта
Filename: "{app}\MKSBridge.exe"; Parameters: "--no-browser --updated"; Flags: nowait; Check: WizardSilent

[Code]
function OpenProcess(dwDesiredAccess: DWORD; bInheritHandle: BOOL; dwProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: DWORD): DWORD;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

{ Обновление из панели: мост запускает установщик с /WAITPID=номер своего процесса и закрывается сам.
  Ждём его здесь, до проверки занятых файлов: иначе установщик застаёт мост закрывающимся и ждёт его 30 секунд. }
function InitializeSetup(): Boolean;
var
  Pid: Integer;
  H: THandle;
begin
  Result := True;
  Pid := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
  if Pid > 0 then
  begin
    H := OpenProcess($00100000, False, Pid);   { SYNCHRONIZE: право только ждать завершения }
    if H <> 0 then
    begin
      WaitForSingleObject(H, 20000);
      CloseHandle(H);
    end;
  end;
end;

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM MKSBridge.exe"; Flags: runhidden; RunOnceId: "StopBridge"
