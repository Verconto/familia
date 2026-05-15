# Grocery Shopping (VkusVill MCP)

When the user asks to buy groceries, order food, or build a shopping cart, the official VkusVill MCP server is available (tools prefixed `vkusvill_`). Auth is anonymous — you do not need user credentials; the user pays in their own account by opening the link you return.

## Flow

1. **Plan ingredients.** If the user names a dish or meal (e.g. "pasta carbonara for three"), first try `vkusvill_recipes` to fetch a matching recipe and use its ingredient list. If no good match, plan ingredients yourself with sensible quantities per person.
2. **Search the full catalog.** For each ingredient call `vkusvill_products_search` with **`vvonly=0`**. The default `vvonly=1` restricts results to VkusVill house brand only and will hide Barilla, Parmalat, Kotanyi, and other third-party brands — which usually breaks recipes that need a specific product.
3. **Pick wisely.** Match cooking style (cured/smoked vs cooked-smoked for bacon, grated vs block for cheese, pasta shape vs dish). Use `vkusvill_product_details` to verify composition when the user has dietary constraints. Use `vkusvill_product_analogs` to suggest replacements when an item is unavailable.
4. **Confirm before generating.** Show the user a preview of the proposed cart — items, quantities, prices, total — and ask for explicit confirmation. When there are several reasonable choices or quantities need guessing, briefly list the top 2-3 options and ask, rather than deciding silently.
5. **Generate the link.** After confirmation, call `vkusvill_cart_link_create` with `{xml_id, q}` for each item and send the resulting link to the user.
6. **Hand off payment.** Never attempt to check out, pay, or place the order yourself. Be explicit: "open this link in your browser/app, you'll see the cart pre-filled in your VkusVill account, complete payment there."

## Limits

- `vkusvill_cart_link_create` accepts at most 30 items per call
- Quantities are floats, range 0.01..40
- All search/discount/recipe endpoints paginate at 10 per page; use `page` for more
