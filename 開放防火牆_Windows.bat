@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 需要系統管理員權限，請在跳出的視窗按「是」
  powershell -Command "Start-Process -FilePath %~f0 -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="Battleship TCP 9100" >nul 2>&1
netsh advfirewall firewall delete rule name="Battleship UDP 9101" >nul 2>&1
netsh advfirewall firewall add rule name="Battleship TCP 9100" dir=in action=allow protocol=TCP localport=9100 profile=any
netsh advfirewall firewall add rule name="Battleship UDP 9101" dir=in action=allow protocol=UDP localport=9101 profile=any
echo.
echo 完成！已開放 TCP 9100（遊戲連線）與 UDP 9101（自動搜尋）
echo 之後當主機的電腦就不會再被防火牆擋住了。
pause
