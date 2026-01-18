# Email Bill Extraction - Amazon Order Support

## Overview

The `mail extract-bill` command uses AI to automatically extract bill/invoice/order information from emails and add them to your budget tracking system.

## Recent Improvements (2026-01-17)

Enhanced support for **Amazon order emails** and other complex order formats.

### What Was Fixed

1. **Price Format Parsing**: Now handles weird Amazon price formats:
   - `$ 15 99` → $15.99 (spaced dollars and cents)
   - `$1599` → $15.99 (concatenated price at line end)
   - `$15.99` → $15.99 (standard format)

2. **Order vs Bill Detection**: LLM now understands:
   - Online orders (Amazon, eBay, etc.)
   - Traditional utility bills
   - Subscription payments
   - Invoices

3. **Smart Naming**: Product names for orders, company names for bills:
   - Amazon orders → Product name (e.g., "Poppy Playtime - Smiling Critters")
   - Utility bills → Company name (e.g., "PG&E Electric")
   - Subscriptions → Service name (e.g., "Netflix")

4. **Better Validation**:
   - Validates amount is a positive number
   - Clear error messages if extraction fails
   - Suggests manual `budget add` command as fallback

## Usage

```bash
mail extract-bill <account> <message-id>
```

### Examples

**Extract from Amazon order:**
```bash
mail inbox                        # List messages
mail extract-bill gmail 123      # Extract bill from message #123
```

**Extract with folder:**
```bash
mail extract-bill work INBOX.Orders:456
```

## How It Works

1. **Pre-processing**: Email content is normalized
   - Weird price formats are fixed before AI processing
   - Helps LLM parse amounts correctly

2. **AI Extraction**: LLM analyzes email for:
   - Product/service name
   - Total amount/price
   - Due date (if applicable)

3. **Validation**: Amount is checked
   - Must be positive number
   - Error if invalid format

4. **Budget Integration**: Automatically adds to budget system
   - Same as `budget add <name> <amount>`
   - Shows confirmation with amount and due date

## Supported Email Types

### ✅ Online Orders
- Amazon
- eBay
- Walmart
- Target
- Etsy
- Any online store with order confirmation

### ✅ Utility Bills
- Electric (PG&E, Edison, etc.)
- Water/Sewer
- Gas
- Internet/Cable
- Phone

### ✅ Subscriptions
- Netflix
- Spotify
- Adobe Creative Cloud
- Microsoft 365
- Any recurring subscription

### ✅ Invoices
- Freelance invoices
- Business invoices
- Service invoices

### ❌ Not Supported
- Shipping notifications without prices
- Order tracking updates
- General promotional emails
- Non-financial emails

## Example: Amazon Order Email

**Original email:**
```
Thanks for your order, Phillip!

Order # 112-5991102-0981861

Poppy Playtime - Smiling Critters

Quantity: 1

$ 15 99
$1599
```

**Extraction result:**
```json
{
  "name": "Poppy Playtime - Smiling Critters",
  "amount": 15.99,
  "due_date": null
}
```

**Bot response:**
```
✅ Bill added from email: **Poppy Playtime - Smiling Critters**

💵 Amount: $15.99
```

## Error Handling

### "This email does not appear to contain bill or invoice information"
- The email is not a financial document
- Try a different email or add manually

### "Extracted bill 'X' but amount (Y) is invalid"
- LLM found the bill but couldn't parse amount
- Use manual command: `budget add <name> <amount>`

### "Could not extract bill details from email"
- AI response was not valid JSON
- Email format is too unusual
- Add manually with `budget add`

## Tips for Best Results

1. **Use recent emails**: Older emails may have different formats
2. **Complete emails**: Forward complete emails, not snippets
3. **Clear amounts**: Emails with clear price/total fields work best
4. **Order confirmations**: Work better than shipping notifications

## Manual Fallback

If extraction fails, you can always add manually:

```bash
budget add "Poppy Playtime Toy" 15.99
```

Or with due date (future feature):
```bash
budget add "Electric Bill" 145.50
```

## Technical Details

### Price Normalization (Pre-processing)

Before AI analysis, these regex patterns fix common issues:

1. **Spaced prices**: `\$ 15 99` → `$15.99`
   ```regex
   \$\s*(\d+)\s+(\d{2}) → $\1.\2
   ```

2. **Concatenated prices**: `$1599` → `$15.99`
   ```regex
   \$(\d{2,})(\d{2})(?!\d) → $\1.\2
   ```

### LLM Prompt Structure

The AI is given:
- Email sender, subject, date
- Normalized body content
- Examples of price formats to look for
- Instructions for name priority (product vs company)

### JSON Schema

Expected AI response:
```json
{
  "name": "string (product/company name)",
  "amount": number (decimal, e.g., 15.99),
  "due_date": "YYYY-MM-DD or null"
}
```

Or error response:
```json
{
  "error": "not_a_bill"
}
```

## Future Improvements

- [ ] Auto-detect bills in inbox (scan new emails)
- [ ] Support for multiple items in one email
- [ ] Handle sales tax separately
- [ ] Extract shipping costs
- [ ] Support for payment confirmations (already paid)
- [ ] Integration with calendar for due dates
- [ ] Recurring bill detection

## Related Commands

- `mail inbox` - List recent emails
- `mail read <account> <id>` - Read full email
- `mail summary <account> <id>` - AI summary of email
- `budget` - View budget and bills
- `budget add <name> <amount>` - Manually add bill
- `budget pay <name>` - Mark bill as paid

## Changelog

### 2026-01-17
- ✅ Added Amazon order support
- ✅ Fixed spaced price format (`$ 15 99`)
- ✅ Fixed concatenated price format (`$1599`)
- ✅ Enhanced LLM prompt with examples
- ✅ Added amount validation
- ✅ Better error messages

### Previous
- Initial implementation for basic bills/invoices
