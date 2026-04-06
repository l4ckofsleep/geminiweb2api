@echo off
title Geminiweb2api - Переавторизация
echo Запуск Geminiweb2api (Reauth Mode)...
echo ВНИМАНИЕ: Старая сессия будет удалена!
python start.py --reauth
pause