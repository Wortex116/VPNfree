# ==================== РЕФЕРАЛЬНАЯ СИСТЕМА (ИСПРАВЛЕННАЯ) ====================

@bot.message_handler(commands=['start'])
def cmd_start(message):
    update_activity()
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Бот работает только в личных сообщениях.")
        return

    user_id = message.from_user.id
    current_time = int(time.time())

    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return

    # 🔥 ИСПРАВЛЕНИЕ: Парсим реферальную ссылку ДО проверки существования пользователя
    referrer_id = None
    if message.text and 'start=ref_' in message.text:
        parts = message.text.split('start=ref_')
        if len(parts) > 1:
            try:
                ref = int(parts[1].strip())
                if ref != user_id:  # Нельзя пригласить самого себя
                    referrer_id = ref
                    print(f"[referral] Найден реферер: {referrer_id} для пользователя {user_id}")
            except:
                pass

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
    existing_user = cur.fetchone()
    cur.close()
    conn.close()

    is_new_user = existing_user is None

    if is_new_user:
        ok, msg = check_subscribe_rate()
        if not ok:
            bot.reply_to(message, f"⚠️ {msg}")
            return

        add_subscribe_record(user_id)

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Я НЕ РОБОТ", callback_data=f"captcha_verify_{user_id}"))

        msg = bot.reply_to(
            message,
            "🤖 *Пожалуйста, подтвердите, что вы не робот*\n\n"
            "Нажмите кнопку ниже для проверки.\n"
            f"⏱ У вас {CAPTCHA_TIMEOUT//60} минут.",
            parse_mode="Markdown",
            reply_markup=kb
        )

        captcha_sessions[user_id] = {
            'timestamp': int(time.time()),
            'message_id': msg.message_id,
            'referrer_id': referrer_id,  # 🔥 СОХРАНЯЕМ РЕФЕРЕРА
            'waiting_for_sub': False
        }
        return

    if not is_subscribed(user_id):
        bot.reply_to(message, "⚠️ Подпишитесь на канал, чтобы пользоваться ботом.", reply_markup=subscribe_button())
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_activity FROM users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    if result:
        last_activity = result[0] or 0
        days_since_last = (current_time - last_activity) // (24 * 60 * 60)
        welcome_text = "👋 С возвращением!" if days_since_last >= 3 else "👋 Добро пожаловать!"
        cur.execute("UPDATE users SET last_activity = %s WHERE user_id = %s", (current_time, user_id))
        conn.commit()
        bot.reply_to(message, welcome_text)
    cur.close()
    conn.close()

    bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())


@bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_verify_'))
def callback_captcha_verify(call):
    user_id = int(call.data.split('_')[2])

    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, "❌ Это не ваша капча.")
        return

    if user_id not in captcha_sessions:
        bot.answer_callback_query(call.id, "❌ Сессия истекла. Нажмите /start")
        return

    session = captcha_sessions[user_id]
    current_time = int(time.time())

    if current_time - session['timestamp'] > CAPTCHA_TIMEOUT:
        del captcha_sessions[user_id]
        bot.answer_callback_query(call.id, "⏰ Время вышло. Нажмите /start")
        return

    try:
        bot.delete_message(call.message.chat.id, session['message_id'])
    except:
        pass

    bot.answer_callback_query(call.id, "✅ Капча пройдена!")

    # 🔥 ПРОВЕРЯЕМ ПОДПИСКУ ПОСЛЕ КАПЧИ
    if is_subscribed(user_id):
        bot.send_message(user_id, "✅ Подписка подтверждена! Регистрируем вас...")
        _register_user(user_id, session.get('referrer_id'))  # 🔥 ПЕРЕДАЕМ РЕФЕРЕРА
        del captcha_sessions[user_id]
    else:
        bot.send_message(
            user_id,
            "⚠️ Подпишитесь на канал, чтобы завершить регистрацию.\n\n"
            "После подписки нажмите кнопку ниже.",
            reply_markup=subscribe_button()
        )
        captcha_sessions[user_id]['waiting_for_sub'] = True


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def callback_check_sub(call):
    update_activity()
    if call.message.chat.type != 'private':
        bot.answer_callback_query(call.id, "⚠️ Работает только в личных сообщениях.")
        return

    user_id = call.from_user.id
    current_time = int(time.time())

    if is_blocked(user_id):
        bot.answer_callback_query(call.id, "🚫 Вы заблокированы.")
        return

    if is_subscribed(user_id):
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass

        # 🔥 ЕСЛИ ПОЛЬЗОВАТЕЛЬ ЖДАЛ ПОДПИСКУ ПОСЛЕ КАПЧИ
        if user_id in captcha_sessions and captcha_sessions[user_id].get('waiting_for_sub'):
            session = captcha_sessions[user_id]
            bot.send_message(user_id, "✅ Подписка подтверждена! Регистрируем вас...")
            _register_user(user_id, session.get('referrer_id'))  # 🔥 ПЕРЕДАЕМ РЕФЕРЕРА
            del captcha_sessions[user_id]
            return

        # 🔥 ЕСЛИ ПОЛЬЗОВАТЕЛЬ УЖЕ ЗАРЕГИСТРИРОВАН - ПРОВЕРЯЕМ РЕФЕРАЛЬНЫЕ БОНУСЫ
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = %s AND rewarded = 0",
            (user_id,)
        )
        pending = cur.fetchone()
        cur.close()
        conn.close()

        if pending and get_setting('referral_enabled') == '1':
            referrer_id = pending[0]
            if is_subscribed(referrer_id):
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (referrer_id,))
                ref_result = cur.fetchone()
                if ref_result:
                    new_end = ref_result[0] + 3 * 24 * 60 * 60
                    cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (new_end, referrer_id))
                    cur.execute("UPDATE referrals SET rewarded = 1 WHERE referred_id = %s", (user_id,))
                    conn.commit()
                    try:
                        bot.send_message(referrer_id, "🎉 Ваш реферал подтвердил подписку! Вам начислено +3 дня.")
                    except:
                        pass
                cur.close()
                conn.close()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        user_exists = cur.fetchone()
        cur.close()
        conn.close()

        if not user_exists:
            _register_user(user_id, None)
        else:
            bot.send_message(user_id, "👋 Добро пожаловать!")
            bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())
    else:
        bot.answer_callback_query(call.id, "❌ Вы ещё не подписались на канал!")


def _register_user(user_id, referrer_id=None):
    """Регистрация пользователя с реферальной системой"""
    current_time = int(time.time())

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return

    token = generate_subscription_token()
    sub_end = current_time + 7 * 24 * 60 * 60

    cur.execute(
        "INSERT INTO users (user_id, subscription_end, last_activity, is_blocked, token) VALUES (%s, %s, %s, 0, %s)",
        (user_id, sub_end, current_time, token)
    )
    conn.commit()
    cur.close()
    conn.close()

    # 🔥 ОБРАБОТКА РЕФЕРАЛЬНОЙ ССЫЛКИ
    if referrer_id:
        print(f"[referral] Попытка регистрации реферала {user_id} от {referrer_id}")
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = %s", (referrer_id,))
        referrer_exists = cur.fetchone()
        cur.close()
        conn.close()

        if referrer_exists and referrer_id != user_id:
            # Проверяем не регистрировался ли уже этот пользователь по рефералке
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM referrals WHERE referrer_id = %s AND referred_id = %s",
                (referrer_id, user_id)
            )
            already_ref = cur.fetchone()
            cur.close()
            conn.close()

            if not already_ref:
                # Проверяем лимит рефералов в день
                if can_add_referral(referrer_id):
                    # Сохраняем реферала
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO referrals (referrer_id, referred_id, reward_date, rewarded) VALUES (%s, %s, %s, 0)",
                        (referrer_id, user_id, current_time)
                    )
                    conn.commit()
                    cur.close()
                    conn.close()
                    print(f"[referral] Реферал сохранен: {referrer_id} -> {user_id}")

                    # Уведомляем реферера
                    name = get_user_display_name(user_id)
                    try:
                        bot.send_message(referrer_id, f"🔔 Новый реферал! Пользователь {name} зарегистрировался по вашей ссылке.")
                    except:
                        pass

                    # 🔥 НАЧИСЛЯЕМ БОНУСЫ ТОЛЬКО ЕСЛИ РЕФЕРЕР ПОДПИСАН НА КАНАЛ
                    if get_setting('referral_enabled') == '1' and is_subscribed(referrer_id):
                        conn = get_db_connection()
                        cur = conn.cursor()
                        cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (referrer_id,))
                        ref_result = cur.fetchone()
                        if ref_result:
                            new_end = ref_result[0] + 3 * 24 * 60 * 60
                            cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (new_end, referrer_id))
                            cur.execute(
                                "UPDATE referrals SET rewarded = 1 WHERE referrer_id = %s AND referred_id = %s",
                                (referrer_id, user_id)
                            )
                            conn.commit()
                            try:
                                bot.send_message(referrer_id, "🎉 Вам начислено +3 дня за нового реферала!")
                            except:
                                pass
                            print(f"[referral] Начислено +3 дня рефереру {referrer_id}")
                        cur.close()
                        conn.close()
                    else:
                        print(f"[referral] Реферер {referrer_id} не подписан на канал или рефералы отключены")
                else:
                    try:
                        bot.send_message(referrer_id, "⚠️ Лимит рефералов (10 в день). Попробуйте завтра.")
                    except:
                        pass
                    print(f"[referral] Лимит рефералов для {referrer_id} превышен")

    bot.send_message(user_id, "🎉 Добро пожаловать! Вам выдана подписка на 7 дней.")
    bot.send_message(user_id, "Выберите действие:", reply_markup=main_menu())


# 🔥 ДОБАВЛЯЕМ КОМАНДУ ДЛЯ ПРОВЕРКИ РЕФЕРАЛЬНОЙ ССЫЛКИ
@bot.message_handler(commands=['ref'])
def cmd_ref_link(message):
    """Показать реферальную ссылку"""
    user_id = message.from_user.id
    if is_blocked(user_id):
        bot.reply_to(message, blocked_message())
        return
    
    bot_username = bot.get_me().username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = %s", (user_id,))
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    
    text = f"""🔗 *Ваша реферальная ссылка:*

`{ref_link}`

📊 Всего рефералов: {total}
📅 Лимит: 10 в день

📌 *Как это работает:*
• Пригласите друга по ссылке
• Он должен зарегистрироваться и подписаться на канал
• Вы получите +3 дня подписки

💬 Поддержка: {SUPPORT}"""
    
    bot.reply_to(message, text, parse_mode="Markdown")
