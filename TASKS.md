# TASKS

מסמך מעקב משימות חי. סמן `[x]` כשמשימה הושלמה; הוסף משימות חדשות מתחת תוך כדי עבודה.

## Todo
- [ ] `cc-history serve` / v2 items (out of scope for v1)
- [ ] Entry-point shim + PATH update (Appendix B) — currently invoked as `python cc_history.py …`
- [ ] בדיקה על macOS (LaunchAgent) — נבדק רק על Windows עד כה

## In progress
- (אין)

## Done
- [x] אתחול הפרויקט + git repo תחת `C:\Users\orben\OneDrive\DEV\Projects\cc-history`
- [x] `cc_history.py` v1 מלא — Paths, סכמת DB + FTS5, פרסר JSONL, indexer אינקרמנטלי, search/show/stats, שני backends של Scheduler, CLI
- [x] `tests/fixtures/sample-session.jsonl` — מכסה כל סוגי הבלוקים (text/thinking/tool_use/tool_result/image-skip/stringified)
- [x] `tests/test_indexer.py` — 17 בדיקות, כולן עוברות
- [x] README.md, LICENSE (MIT), .gitignore, install.sh, install.ps1
- [x] סבב בדיקות שפיות מלא על Windows: index (אינקרמנטלי), stats, search, show, --json, חיפוש עברית, install/kickstart/uninstall
- [x] תיקון באג אמיתי: `<LogonTrigger>` בלי `<UserId>` גרם ל-`Access is denied`. תוקן ב-`cc_history.py` + עודכן האיפיון §8b
- [x] תיקון: כפיית UTF-8 על stdout/stderr ב-Windows כדי שפלט עברית/`·`/`…` לא יקרוס על cp1252

## Notes
- מפרש Python בשימוש: `C:\Users\orben\AppData\Local\Programs\Python\Python311\python.exe` (ה-`python` ב-PATH הוא ה-stub של חנות Microsoft ולא עובד).
- אינדקס נוכחי: ~3,575 רשומות מ-19 קבצים, 6 פרויקטים.
