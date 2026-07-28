# TWEAKOS Echo Bot — aiogram 3.x
# pip3 install aiogram

import asyncio
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import Message

TOKEN = "8990944675:AAHLXBWtTFfnY_X8fUPmIVi5w3sMw1c1AdM"
ADMIN_IDS = [8176196456]

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

@router.message(Command("start"))
async def start(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Бот работает. /ping /echo /id")

@router.message(Command("ping"))
async def ping(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("pong")

@router.message(Command("echo"))
async def echo_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        await message.answer("Использование: /echo текст")
        return
    await message.answer(text[1])

@router.message(Command("id"))
async def id_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(f"Твой ID: {message.from_user.id}")

@router.message()
async def echo(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(f"Эхо: {message.text}")

async def main():
    print("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
