from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().with_name(".env"))

import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
from datetime import datetime

# ==========================================
# 🔑 CONFIGURATION
# ==========================================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "")

# ==========================================
# 📊 GOOGLE SHEETS SETUP
# ==========================================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_sheet():
    """Forces a completely fresh connection to Google every time it is called."""
    try:
        fresh_creds = ServiceAccountCredentials.from_json_keyfile_name(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"), scope)
        fresh_client = gspread.authorize(fresh_creds)
        return fresh_client.open(GOOGLE_SHEET_NAME).sheet1
    except Exception as e:
        print(f"⚠️ Failed to open Google Sheet: {e}")
        raise e

# ==========================================
# 🤖 BOT SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command(name="bought")
async def log_purchase(ctx, price: str = None):
    """Reply to an alert: !bought <paid_price>"""
    if not ctx.message.reference or not ctx.message.reference.message_id:
        await ctx.reply("❌ Reply to an alert with `!bought <price>`.", delete_after=5)
        return

    if not price:
        await ctx.reply("❌ Missing price.", delete_after=5)
        return
        
    try:
        paid_price = float(price.replace('$', '').replace(',', '').strip())
    except ValueError:
        await ctx.reply("❌ Invalid price format.", delete_after=5)
        return

    try:
        referenced_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        embed = referenced_msg.embeds[0]
    except Exception:
        await ctx.reply("❌ Could not read the original alert embed.", delete_after=5)
        return

    title = embed.title.replace("🚨 DEAL FOUND: ", "").replace("🚨 DEEP SWEEP DEAL FOUND: ", "") if embed.title else "Unknown Card"
    
    cert, grader = "Unknown", "Unknown"
    cl_value = 0.0

    for field in embed.fields:
        if "Cert #" in field.name:
            grader = field.name.replace(" Cert #", "").strip()
            cert = field.value.strip()
        elif "Card Ladder Comp" in field.name:
            clean_val = re.sub(r'[^\d.]', '', field.value)
            cl_value = float(clean_val) if clean_val else 0.0

    # ==========================================
    # 🚀 PUSH TO GOOGLE SHEETS
    # ==========================================
    try:
        sheet = get_sheet()
        
        # Find the next available row based on Date column
        col_a_values = sheet.col_values(1)
        next_row = len(col_a_values) + 1
        
        date_str = datetime.now().strftime("%Y-%m-%d")
        
        # Column H: Net Profit (Calculates only when a Sold Price is typed into Col G)
        net_profit_formula = f'=IF(ISBLANK(G{next_row}), "", (G{next_row}*0.9)-E{next_row})'
        
        # Maps strictly across columns A through I
        row_data = [
            date_str,            # Col A: Date
            title,               # Col B: Title
            cert,                # Col C: Cert
            grader,              # Col D: Grader
            paid_price,          # Col E: Paid Price
            cl_value,            # Col F: CL Price
            "",                  # Col G: Sold Price (Left blank for you/app)
            net_profit_formula,  # Col H: Net Profit Formula
            "FALSE",             # Col I: Sold Checkbox
        ]
        
        # Explicitly write across range A to I (Leaving J+ untouched)
        sheet.update(
            range_name=f"A{next_row}:I{next_row}",
            values=[row_data],
            value_input_option="USER_ENTERED",
        )
        
        await ctx.message.add_reaction("✅")
        await ctx.reply(f"📈 **Sent to Row {next_row}!**\n• Card: {title[:30]}...\n• Paid: ${paid_price:.2f} | CL Comp: ${cl_value:.2f}")
        
    except Exception as e:
        await ctx.reply(f"❌ **Google Sheets Error:** {e}")

@bot.event
async def on_ready():
    print(f"✅ Google Sheets Link Active. Logged in as {bot.user.name}")
    print("👉 Waiting for !bought commands...")

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)