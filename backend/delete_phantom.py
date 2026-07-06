import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.db.database import AsyncSessionLocal
from app.db.models import Contact, ContactInteraction
from sqlalchemy import delete

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ContactInteraction).where(ContactInteraction.contact_id == '42286854-f7e2-4a17-995b-a78d27319bf6'))
        await db.execute(delete(Contact).where(Contact.id == '42286854-f7e2-4a17-995b-a78d27319bf6'))
        await db.commit()
        print("Deleted phantom contact Jamil")

if __name__ == "__main__":
    asyncio.run(main())
