"""JavaScript/TypeScript extraction: line ranges, calls and callers, and the syntax that trips naive parsers."""

import textwrap

from knock.extractors import extract_file
from knock.service import KnockService



def ex(path, text, language="typescript"):
    return extract_file(path, textwrap.dedent(text).lstrip("\n"), language, "code")


def syms(result):
    return {s.name: s for s in result.symbols}


SERVICE_TS = """
import { Injectable } from "@nestjs/common";
import { UserRepository } from "./user.repository";

/** Loads and caches users. Talks to the database through UserRepository. */
@Injectable()
export class UserService extends BaseService implements OnModuleInit {
  private readonly cache = new Map<string, User>();
  static readonly MAX_RETRIES = 3
  timeout: number

  constructor(private repo: UserRepository) {
    super();
  }

  /**
   * Find a user by id, retrying on timeouts.
   * @param id the user id
   */
  async findById(id: string): Promise<{ user: User | null }> {
    try {
      const user = await this.repo.load(id, process.env.DB_SCHEMA);
      return { user };
    } catch (err) {
      if (err instanceof TimeoutError) {
        return this.retry(id, UserService.MAX_RETRIES);
      }
      throw new NotFoundError(`user ${id} not in ${"db"}`);
    }
  }

  private retry = async (id: string, times: number): Promise<{ user: User | null }> => {
    logRetry(id, times);
    return this.findById(id);
  };

  get size(): number {
    return this.cache.size;
  }

  abstract validate(u: User): boolean;
}

export function overloaded(a: string): string;
export function overloaded(a: number): number;
export function overloaded(a: any): any {
  return normalize(a);
}

export interface User extends Entity {
  id: string;
  email: string;
}

export type Role =
  | "admin"
  | "member";

export enum Status { Active = "active", Disabled = "disabled" }

export const DEFAULT_ROLE: Role = "member";
"""


def test_typescript_class_methods_ranges_and_refs():
    r = ex("src/users/user.service.ts", SERVICE_TS)
    s = syms(r)
    assert {"UserService", "UserService.constructor", "UserService.findById", "UserService.retry", "UserService.size",
            "overloaded", "User", "Role", "Status", "DEFAULT_ROLE"} <= set(s)
    assert "UserService.validate" not in s          # abstract: no body
    cls = s["UserService"]
    assert cls.line == 5 and cls.end_line == 41       # from the decorator to the closing brace
    assert {"BaseService", "OnModuleInit"} <= set(cls.refs)
    assert cls.doc.startswith("Loads and caches users.")
    find = s["UserService.findById"]
    assert (find.line, find.end_line) == (19, 29)
    assert find.doc == "Find a user by id, retrying on timeouts."
    assert {"load", "DB_SCHEMA", "retry", "MAX_RETRIES", "NotFoundError"} <= set(find.refs)
    assert find.catches == ["TimeoutError"]
    assert "findById(id: string)" in find.signature
    retry = s["UserService.retry"]
    assert (retry.line, retry.end_line) == (31, 34) and {"logRetry", "findById"} <= set(retry.refs)
    over = s["overloaded"]
    assert over.line == 45 and "normalize" in over.refs   # the implementation, not the overloads
    assert s["Role"].kind == "type" and s["Role"].end_line == 56
    assert s["User"].kind == "type" and "Entity" in s["User"].refs
    assert s["DEFAULT_ROLE"].kind == "constant"


COMPONENT_TSX = """
import React, { useState, memo, forwardRef } from "react";

// Shows one order. Don't render it for drafts: it'll crash on {missing} totals.
export default function OrderCard({ order }: { order: Order }) {
  const [open, setOpen] = useState(false);
  const label = `Order ${order.id} – ${order.items.map((i) => i.name).join(", ")} {not a brace}`;
  const re = /[{}'"]+/g;
  if (!order) return null;
  return (
    <Card title={label} onClick={() => trackClick(order.id)}>
      <p>Don't forget: totals > 0 {order.total > 0 ? "paid" : 'unpaid'}</p>
      {open && <OrderItems items={order.items} onRemove={(id) => removeItem(id)} />}
      <>
        <Price value={formatMoney(order.total)} />
      </>
    </Card>
  );
}

export const OrderList = memo(function OrderList({ orders }: Props) {
  return <ul>{orders.map((o) => <OrderCard key={o.id} order={o} />)}</ul>;
});

export const Field = forwardRef<HTMLInputElement, FieldProps>((props, ref) => (
  <input ref={ref} {...props} onChange={(e) => validateField(e.target.value)} />
));

const identity = <T,>(x: T): T => x;
"""


def test_tsx_components_jsx_and_tricky_literals():
    r = ex("web/components/OrderCard.tsx", COMPONENT_TSX)
    s = syms(r)
    card = s["OrderCard"]
    assert (card.line, card.end_line) == (4, 18)
    assert card.doc.startswith("Shows one order.")
    assert {"Card", "trackClick", "OrderItems", "removeItem", "Price", "formatMoney"} <= set(card.refs)
    assert "useState" not in card.refs                # React built-ins are noise
    lst = s["OrderList"]
    assert lst.kind == "function" and lst.end_line == 22 and "OrderCard" in lst.refs
    field = s["Field"]
    assert field.kind == "function" and (field.line, field.end_line) == (24, 26) and "validateField" in field.refs
    assert s["identity"].kind == "function"           # <T,> is a generic, not JSX


ROUTES_JS = """
const express = require("express");
const router = express.Router();
const { requireAuth } = require("../auth");

router.get("/orders/:id", requireAuth, async (req, res) => {
  const order = await loadOrder(req.params.id);
  res.json(serializeOrder(order));
});

router.post("/orders", requireAuth, orders.create);

function registerAdmin(app) {
  app.delete("/admin/orders/:id", purgeOrder);
}

module.exports.handler = async (event) => {
  return processEvent(event);
};

module.exports = { registerAdmin };
"""


def test_express_routes_and_commonjs():
    r = ex("server/routes/orders.js", ROUTES_JS, "javascript")
    s = syms(r)
    get = s["GET /orders/:id"]
    assert get.kind == "route" and (get.line, get.end_line) == (5, 8)
    assert {"requireAuth", "loadOrder", "serializeOrder"} <= set(get.refs)
    assert {"requireAuth", "create"} <= set(s["POST /orders"].refs)
    assert "DELETE /admin/orders/:id" in s                     # nested route, found by line pattern
    assert s["registerAdmin"].end_line == 14
    assert "processEvent" in s["handler"].refs
    assert s["router"].kind == "variable" and "Router" in s["router"].refs


def test_test_blocks_and_vue_sfc():
    t = ex("web/orders.test.ts", """
        import { describe, it, expect } from "vitest";

        describe("formatMoney", () => {
          it("rounds", () => {
            expect(formatMoney(1.005)).toBe("$1.01");
          });
        });
        """)
    block = syms(t)["describe: formatMoney"]
    assert block.kind == "test" and block.end_line == 7 and block.refs == ["formatMoney"]

    vue = ex("web/components/Cart.vue", """
        <template>
          <div @click="checkout">{{ total }}</div>
        </template>

        <script lang="ts">
        import { defineComponent } from "vue";
        export default defineComponent({
          methods: {
            checkout() {
              submitOrder(this.items);
            },
          },
        });
        </script>
        """, "vue")
    s = syms(vue)
    assert s["Cart.checkout"].line == 9 and "submitOrder" in s["Cart.checkout"].refs


def test_minified_and_broken_files_fall_back_to_line_patterns():
    minified = "function a(){return b()}" + ";var x=1" * 2000 + "\nexport function keepMe() {}\n"
    r = ex("web/vendor.min.js", minified, "javascript")
    assert "a" in syms(r) or "keepMe" in syms(r)      # line patterns still work
    broken = ex("web/broken.ts", """
        export function ok() { return 1 }
        const s = `never closed ${
        """)
    assert "ok" in syms(broken)


def test_map_shows_typescript_callers(make_repo, service: KnockService):
    root = make_repo({
        "README.md": "# Shop\n\nA small shop service.\n",
        "src/orders/order.service.ts": """
            export class OrderService {
              async cancelOrder(id: string) {
                const order = await loadOrder(id);
                return refundPayment(order);
              }
            }
            """,
        "src/payments/refund.ts": """
            /** Sends the refund to the payment provider. */
            export async function refundPayment(order: Order) {
              return stripe.refunds.create({ payment_intent: order.paymentId });
            }
            """,
        "src/orders/order.controller.ts": """
            export class OrderController {
              constructor(private orders: OrderService) {}
              cancel(req: Request) {
                return this.orders.cancelOrder(req.params.id);
              }
            }
            """,
    })
    rid = service.register(str(root))["id"]
    service.refresh_repository(rid)
    out = service.code_map("where do we refund the payment when an order is cancelled?", repository=rid)
    assert "refundPayment" in out and "L2-4" in out
    assert "called by cancelOrder" in out
    assert "Sends the refund to the payment provider." in out


def test_ambient_declarations():
    r = ex("src/global.d.ts", """
        declare module "*.scss";

        interface ArrayBuffer {
          _brand?: "ArrayBuffer";
        }

        declare global {
          interface Window { appVersion: string }
        }

        namespace Api.V1 {
          export function ping(): Promise<void> { return send("ping") }
        }
        """)
    s = syms(r)
    assert s["ArrayBuffer"].end_line == 5 and "Window" in s
    assert "send" in s["ping"].refs
