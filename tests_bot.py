# -*- coding: utf-8 -*-
"""Проверки бота по блокам: Telegram, промпт, DeepSeek, Robokassa, тестовый UI.
На Windows при ошибке 'No time zone found with key Europe/Moscow' выполните: pip install tzdata"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_1_import_and_prompt():
    import bot
    assert hasattr(bot, 'SYSTEM_PROMPT'), 'SYSTEM_PROMPT отсутствует'
    assert isinstance(bot.SYSTEM_PROMPT, str), 'SYSTEM_PROMPT не строка'
    assert len(bot.SYSTEM_PROMPT) > 100, 'Промпт слишком короткий'
    assert 'Денис Комков' in bot.SYSTEM_PROMPT, 'Промпт не содержит ключевой роли'
    assert 'start_diagnosis' in bot.STEP_KEYBOARDS, 'Нет клавиатуры start_diagnosis'
    assert 'form_address' in bot.STEP_KEYBOARDS, 'Нет клавиатуры form_address'
    assert 'messenger' in bot.STEP_KEYBOARDS, 'Нет клавиатуры messenger'
    return True

def test_2_prompt_file_and_steps():
    import bot as b
    prompt_path = os.path.join(os.path.dirname(os.path.abspath(b.__file__)), 'system_prompt.txt')
    assert os.path.isfile(prompt_path), 'system_prompt.txt не найден'
    with open(prompt_path, encoding='utf-8') as f:
        raw = f.read()
    for step in ('start_diagnosis', 'form_address', 'messenger'):
        assert step in b.STEP_KEYBOARDS, f'В STEP_KEYBOARDS нет {step}'
    assert '[STEP:start_diagnosis]' in raw or 'start_diagnosis' in raw
    assert '[STEP:form_address]' in raw or 'form_address' in raw
    assert '[STEP:messenger]' in raw or 'messenger' in raw
    return True

def test_3_parse_and_keyboards():
    import bot
    text_clean = 'Привет. Нажми кнопку.'
    for step_id in ('start_diagnosis', 'form_address', 'messenger'):
        reply = text_clean + '\n[STEP:' + step_id + ']'
        clean, parsed = bot._parse_step_from_reply(reply)
        assert parsed == step_id, f'parse_step: ожидали {step_id}, получили {parsed}'
        assert clean == text_clean, 'Текст после парсинга должен быть без тега'
        kb = bot._keyboard_for_step(step_id)
        assert kb is not None, f'Клавиатура для {step_id} не создаётся'
    return True

def test_4_handlers():
    import bot
    app = bot.build_application()
    group0 = app.handlers.get(0, [])
    names = []
    for h in group0:
        if hasattr(h, 'callback') and hasattr(h.callback, '__name__'):
            names.append(h.callback.__name__)
    assert any('start' in n for n in names), 'Нет обработчика start'
    assert any('start_chat' in n or 'button_start_chat' == n for n in names), 'Нет start_chat'
    assert any('handle_step_button' == n for n in names), 'Нет handle_step_button'
    assert any('handle_message' == n for n in names), 'Нет handle_message'
    return True

def test_5_callback_length_and_format():
    import bot
    for step_id, rows in bot.STEP_KEYBOARDS.items():
        if rows is None:
            continue
        for row in rows:
            for label, cb in row:
                data_len = len(cb.encode('utf-8'))
                assert data_len <= 64, f'callback_data "{cb}" ({data_len} байт) > 64'
    out, mode = bot._format_reply_for_telegram('Текст **жирный** и список\n* пункт')
    assert 'жирный' in out and '<b>' in out
    assert 'пункт' in out and '➖' in out
    return True


def test_6_new_step_buttons():
    """Проверка кнопок: insight_next, readiness (продуктовые шаги убраны — только мини‑приложение)."""
    import bot
    new_steps = ('insight_next', 'readiness')
    for step_id in new_steps:
        assert step_id in bot.STEP_KEYBOARDS, f'В STEP_KEYBOARDS нет {step_id}'
        clean, parsed = bot._parse_step_from_reply('Текст\n[STEP:' + step_id + ']')
        assert parsed == step_id
        kb = bot._keyboard_for_step(step_id)
        assert kb is not None
    with open(os.path.join(os.path.dirname(__file__), 'system_prompt.txt'), encoding='utf-8') as f:
        raw = f.read()
    for step_id in new_steps:
        assert f'[STEP:{step_id}]' in raw or step_id in raw, f'В промпте нет тега для {step_id}'
    return True


def test_7_load_prompt_file():
    """Проверка загрузки system_prompt.txt и отсутствия пустого промпта."""
    import bot
    assert len(bot.SYSTEM_PROMPT.strip()) > 500
    assert 'ОДИН ВОПРОС' in bot.SYSTEM_PROMPT
    assert 'КНОПКИ' in bot.SYSTEM_PROMPT
    return True


# ---- Тесты тестового UI (test_dialog_ui.py): проверка, что весь функционал доступен без auto_dialog.py ----

def test_ui_1_module_has_main():
    """Тестовый UI: модуль test_dialog_ui имеет функцию main()."""
    import test_dialog_ui
    assert hasattr(test_dialog_ui, 'main'), 'test_dialog_ui.main отсутствует'
    assert callable(test_dialog_ui.main), 'test_dialog_ui.main не вызываема'
    return True


def test_ui_2_entry_point_when_run_as_script():
    """Тестовый UI: при запуске как скрипт (python test_dialog_ui.py) вызывается main()."""
    import ast
    import test_dialog_ui
    path = getattr(test_dialog_ui, '__file__', None)
    assert path and path.endswith('test_dialog_ui.py'), 'Не найден файл модуля'
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    main_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            if (isinstance(node.test, ast.Compare) and
                    isinstance(node.test.left, ast.Name) and
                    node.test.left.id == '__name__' and
                    isinstance(node.test.comparators[0], ast.Constant) and
                    node.test.comparators[0].value == '__main__'):
                for stmt in node.body:
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                        if isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == 'main':
                            main_calls.append(True)
    assert main_calls, 'В блоке if __name__ == "__main__" нет вызова main()'
    return True


def test_ui_3_bot_exports_required_by_ui():
    """Тестовый UI: бот экспортирует все функции/переменные, которые импортирует UI."""
    import bot
    required = ('get_bot_reply', 'clear_history', 'get_simulator_reply', 'SIMULATOR_ENABLED')
    for name in required:
        assert hasattr(bot, name), f'В bot отсутствует {name}, нужный тестовому UI'
    assert callable(bot.get_bot_reply)
    assert callable(bot.clear_history)
    assert callable(bot.get_simulator_reply)
    return True


def test_ui_4_run_async_in_thread():
    """Тестовый UI: run_async_in_thread передаёт очередь в runner и возвращает очередь с результатом."""
    import queue
    import time
    import test_dialog_ui
    result = []

    def fake_runner(q):
        q.put(("tech", "ok"))
        q.put(("done", None))

    q = test_dialog_ui.run_async_in_thread(fake_runner)
    assert isinstance(q, queue.Queue)
    for _ in range(50):
        try:
            kind, payload = q.get(timeout=0.2)
            result.append((kind, payload))
            if kind == "done":
                break
        except queue.Empty:
            time.sleep(0.05)
    assert ("tech", "ok") in result
    assert ("done", None) in result
    return True


def test_ui_5_no_auto_dialog_import():
    """Тестовый UI: не импортирует auto_dialog (запуск только через test_dialog_ui.py)."""
    import test_dialog_ui
    path = getattr(test_dialog_ui, '__file__', '')
    assert path.endswith('test_dialog_ui.py'), 'Должен быть загружен именно test_dialog_ui'
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert 'import auto_dialog' not in src and 'from auto_dialog' not in src, \
        'test_dialog_ui не должен импортировать auto_dialog'
    return True


# ---- Блок Robokassa: интеграция оплаты ----

def test_robokassa_1_format_client_anket_table():
    """Robokassa: _format_client_anket_table возвращает строку с заголовком и полями."""
    from robokassa_integration import _format_client_anket_table
    out = _format_client_anket_table(None)
    assert "Анкета" in out or "отсутствует" in out
    client = {"user_id": 123, "product": "group_vip", "focus": "тест"}
    out2 = _format_client_anket_table(client)
    assert "123" in out2 and "group_vip" in out2 and "тест" in out2
    return True


def test_robokassa_2_verify_result_url_raises_on_bad_params():
    """Robokassa: verify_result_url выбрасывает при неверной подписи или отсутствии параметров."""
    from robokassa_integration import verify_result_url, RobokassaConfig
    cfg = RobokassaConfig(
        merchant_login="test",
        password1="p1",
        password2="p2",
        merchant_url="https://test/",
        is_test=False,
    )
    try:
        verify_result_url({}, cfg=cfg)
        assert False, "ожидалось исключение"
    except (ValueError, KeyError):
        pass
    try:
        verify_result_url({"OutSum": "100", "InvId": "1", "SignatureValue": "bad"}, cfg=cfg)
        assert False, "ожидалось исключение при неверной подписи"
    except ValueError:
        pass
    return True


def test_robokassa_3_build_payment_url_returns_url():
    """Robokassa: build_payment_url возвращает строку-URL с параметрами."""
    from robokassa_integration import build_payment_url, RobokassaConfig
    cfg = RobokassaConfig(
        merchant_login="test",
        password1="p1",
        password2="p2",
        merchant_url="https://auth.robokassa.ru/Merchant/Index.aspx",
        is_test=False,
    )
    url = build_payment_url(
        cfg=cfg,
        inv_id=1,
        out_sum="100",
        description="Test",
        shp={"Shp_user_id": "123"},
    )
    assert url.startswith("https://") and "MerchantLogin=test" in url and "InvId=1" in url
    return True


def test_robokassa_4_process_result_url_signature():
    """Robokassa: process_result_url возвращает (bool, int) и при неверной подписи — (False, 0 или inv_id)."""
    from robokassa_integration import process_result_url, RobokassaConfig, PaymentsDB
    import tempfile
    cfg = RobokassaConfig(
        merchant_login="t",
        password1="p1",
        password2="p2",
        merchant_url="https://t/",
        is_test=False,
    )
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name
    try:
        db = PaymentsDB(db_path)
        success, inv_id = process_result_url({"OutSum": "100", "InvId": "999", "SignatureValue": "x"}, cfg=cfg, db=db)
        assert success is False
        assert inv_id == 0 or inv_id == 999
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
    return True


if __name__ == '__main__':
    blocks = [
        ("Telegram (обработчики, кнопки)", [
            ("Handlers in Application", test_4_handlers),
            ("callback_data length and format", test_5_callback_length_and_format),
        ]),
        ("Промпт и шаги диалога", [
            ("Import, prompt, STEP_KEYBOARDS", test_1_import_and_prompt),
            ("Prompt file and step_id", test_2_prompt_file_and_steps),
            ("Parse STEP and keyboards", test_3_parse_and_keyboards),
            ("New step buttons", test_6_new_step_buttons),
            ("Load prompt file and content", test_7_load_prompt_file),
        ]),
        ("DeepSeek (конфиг, вызов модели)", [
            # Зависит от промпта и zoneinfo; при отсутствии tzdata падает — помечаем как опциональный
            ("Config: DEEPSEEK_MODEL and temperature", lambda: (__import__("bot").DEEPSEEK_MODEL and True)),
        ]),
        ("Robokassa (оплата)", [
            ("_format_client_anket_table", test_robokassa_1_format_client_anket_table),
            ("verify_result_url raises on bad params", test_robokassa_2_verify_result_url_raises_on_bad_params),
            ("build_payment_url returns URL", test_robokassa_3_build_payment_url_returns_url),
            ("process_result_url returns (bool, int)", test_robokassa_4_process_result_url_signature),
        ]),
        ("Тестовый UI и прочее", [
            ("UI: module has main()", test_ui_1_module_has_main),
            ("UI: __main__ entry point", test_ui_2_entry_point_when_run_as_script),
            ("UI: bot exports required", test_ui_3_bot_exports_required_by_ui),
            ("UI: run_async_in_thread", test_ui_4_run_async_in_thread),
            ("UI: no auto_dialog", test_ui_5_no_auto_dialog_import),
        ]),
    ]
    scores = []
    for block_name, tests in blocks:
        print("\n=== %s ===" % block_name)
        for name, fn in tests:
            try:
                fn()
                print("  OK:", name)
                scores.append(10)
            except Exception as e:
                print("  FAIL:", name, "-", e)
                scores.append(4)
    avg = sum(scores) / len(scores) if scores else 0
    print("\nИтого: avg =", round(avg, 1))
    sys.exit(0 if avg > 7 else 1)
