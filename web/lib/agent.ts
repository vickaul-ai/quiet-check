import "server-only";

import { readFile } from "node:fs/promises";
import path from "node:path";

import { tool } from "ai";
import { z } from "zod";

import { BackendError, createOrder, getCatalog } from "./backend";
import { REPO_DIR } from "./env";
import type { CartLine, CheckoutResult, Product } from "./types";

export const MAX_QUANTITY = 10; // contracts.md, create-order validation

export async function shoppingPrompt(): Promise<string> {
  return readFile(path.join(REPO_DIR, "prompts/shopping_agent.md"), "utf8");
}

function matches(product: Product, query: string): boolean {
  const name = `${product.name} ${product.sku}`.toLowerCase().replace(/[^a-z0-9]+/g, " ");
  const words = query.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 1);
  return words.every((w) => name.includes(w.replace(/s$/, "")));
}

// One set of tools per chat request. `cart` is the browser's cart (state lives in the page);
// the tools change this copy and return it, and the page takes the new cart from the tool output.
export function shoppingTools({ cart, deviceId }: { cart: CartLine[]; deviceId: string }) {
  const lines = cart.map((line) => ({ ...line }));

  return {
    search_products: tool({
      description:
        "Search the catalogue. Returns matching products with SKU, name and price. " +
        "An empty query, or one with no match, returns the whole catalogue.",
      inputSchema: z.object({ query: z.string().describe("Words to match, e.g. 'blue backpack medium'") }),
      execute: async ({ query }) => {
        const catalog = await getCatalog();
        const found = catalog.filter((p) => matches(p, query));
        const products = (found.length ? found : catalog).map(({ sku, name, unit_amount }) => ({
          sku,
          name,
          price: unit_amount.value,
        }));
        return { exact: found.length > 0, products };
      },
    }),

    add_to_cart: tool({
      description: "Add a product to the buyer's cart by its exact SKU. Returns the whole cart.",
      inputSchema: z.object({
        sku: z.string(),
        quantity: z.number().int().min(1).max(MAX_QUANTITY).default(1),
      }),
      execute: async ({ sku, quantity }) => {
        const product = (await getCatalog()).find((p) => p.sku === sku);
        if (!product) return { error: `Unknown SKU ${sku}. Search first.`, cart: lines };
        const line = lines.find((l) => l.sku === sku);
        if (line) line.quantity = Math.min(MAX_QUANTITY, line.quantity + quantity);
        else lines.push({ sku, quantity });
        return { added: { sku, name: product.name, quantity }, cart: lines };
      },
    }),

    checkout: tool({
      description:
        "Create the PayPal order for everything in the cart. The shop prices it. " +
        "The buyer then approves the payment with the PayPal button.",
      inputSchema: z.object({}),
      execute: async (): Promise<CheckoutResult | { error: string }> => {
        if (!lines.length) return { error: "The cart is empty." };
        try {
          const catalog = await getCatalog();
          const order = await createOrder(lines, deviceId);
          const items = lines.map((l) => ({ ...l, name: catalog.find((p) => p.sku === l.sku)?.name ?? l.sku }));
          return { order_id: order.order_id, paypal_order_id: order.paypal_order_id, amount: order.amount, items };
        } catch (err) {
          if (err instanceof BackendError) return { error: `Checkout failed (${err.code}). Please try again.` };
          throw err;
        }
      },
    }),
  };
}
