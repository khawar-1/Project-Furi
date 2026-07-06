import asyncio
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.db.database import AsyncSessionLocal
from app.db.models import Contact
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Contact))
        contacts = res.scalars().all()
        print(json.dumps([{'id': c.id, 'name': c.name, 'email': c.email, 'phone': c.phone} for c in contacts], indent=2))

if __name__ == "__main__":
    asyncio.run(main())
