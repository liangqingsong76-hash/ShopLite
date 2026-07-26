"""交易查询数值边界的轻量回归测试。"""

from decimal import Decimal

from django.test import SimpleTestCase

from .services import parse_decimal


class DecimalParsingTests(SimpleTestCase):
    """验证外部价格参数只会进入 ORM 可比较的有限小数。"""

    def test_non_finite_decimal_values_are_rejected(self):
        """NaN 与正负无穷不能泄漏到商品价格筛选表达式。"""

        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(parse_decimal(value))

    def test_finite_decimal_value_is_preserved(self):
        """合法有限小数仍按原精度返回给调用方。"""

        self.assertEqual(parse_decimal("12.50"), Decimal("12.50"))
