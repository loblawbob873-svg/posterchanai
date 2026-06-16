"""Auto-split from the original command_service.py monolith (mixin pattern). No behavior change."""


class _FinanceMixin:
    async def _budget_command(self) -> dict:
        from app.services import finance_service
        try:
            base, key = finance_service.get_config(self.db, self.user)
            summary = await finance_service.get_summary(base, key)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": finance_service.format_summary(summary)}

    async def _bills_command(self, arg: str) -> dict:
        from app.services import finance_service
        arg = (arg or "").strip().lower()
        status = None if arg == "all" else (arg if arg in ("paid", "unpaid") else "unpaid")
        header = {"paid": "Paid bills", "unpaid": "Unpaid bills", None: "All bills"}.get(status, "Unpaid bills")
        try:
            base, key = finance_service.get_config(self.db, self.user)
            bills = await finance_service.get_bills(base, key, status=status)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": finance_service.format_bills(bills, header=header)}

    async def _pay_command(self, arg: str) -> dict:
        from app.services import finance_service
        name = (arg or "").strip()
        if not name:
            return {"type": "text", "content": "Usage: pay <bill name>"}
        try:
            base, key = finance_service.get_config(self.db, self.user)
            result = await finance_service.pay_bill(base, key, name)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": f"✅ {result.get('message', 'Paid.')}"}

    async def _addbill_command(self, arg: str) -> dict:
        from app.services import finance_service
        try:
            name, amount, is_income = finance_service.parse_add_bill_arg(arg or "")
            base, key = finance_service.get_config(self.db, self.user)
            result = await finance_service.add_bill(base, key, name, amount, is_income=is_income)
        except finance_service.FinanceError as e:
            return {"type": "text", "content": f"💰 {e}"}
        return {"type": "text", "content": f"✅ {result.get('message', 'Added.')}"}
