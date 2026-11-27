"""Edge cases for the quotation audit fixes (OD-6-CINDY: D2, D3, D4, D5, D9, D10).

``test_cindy_defects_d1_d13`` asserts the defects are gone. These tests assert the fixes do
not over-reach: an addition still adds, a number that is not an offered option is still a
quantity, a document re-send never issues a new quotation, an unlisted product still goes
to staff while the valid part stays open, and the product list stays within its budget.
"""

from __future__ import annotations

import json
import unittest

from platform_admin.services import quotation_flow
from saas import interpretation
from saas import quotation_intent as intent

try:
    from .test_cindy_defects_d1_d13 import CindyCatalogueBase
except ImportError:  # discovery inside the tests directory
    from test_cindy_defects_d1_d13 import CindyCatalogueBase  # type: ignore[no-redef]


class D2_AdditionStillAdds(CindyCatalogueBase):
    def test_actually_add_another_product_appends(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Actually add a Vanilla too")
        names = [i.get("product_name") for i in self._items()]
        self.assertEqual(names, ["Black Forest", "Vanilla"])

    def test_replacement_keeps_quantity(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Actually make it Vanilla")
        items = self._items()
        self.assertEqual([(i.get("product_name"), i.get("quantity")) for i in items],
                         [("Vanilla", 2)])

    def test_replacement_with_an_extra_detail_joined_by_and(self):
        # [review s6-cindy-fixes R1-F1] "and" adding a detail must not block replacement.
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Actually make it Vanilla and 2 kg")
        self.assertEqual(len(self._quotations()), 1, f"bot said {self._texts()}")
        lines = self._lines(self._quotations()[0][0])
        self.assertEqual(len(lines), 1)
        self.assertIn("Vanilla", lines[0][0])
        self.assertEqual(int(lines[0][1]), 2)

    def test_two_products_joined_by_and_are_both_kept(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Actually I want Vanilla and Chocolate")
        names = [i.get("product_name") for i in self._items()]
        self.assertEqual(names, ["Black Forest", "Vanilla", "Chocolate"])


class D3_PendingOption(CindyCatalogueBase):
    def test_bare_size_number_completes_and_keeps_quantity(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("2")
        quotations = self._quotations()
        self.assertEqual(len(quotations), 1, f"bot said {self._texts()}")
        lines = self._lines(quotations[0][0])
        self.assertEqual([int(line[1]) for line in lines], [2])
        self.assertIn("2", lines[0][0])  # the 2 kg variant was chosen

    def test_number_outside_the_options_is_still_a_quantity(self):
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("7")  # only 1/2/3 kg are offered: 7 is a count
        items = self._items()
        self.assertEqual(items[0].get("quantity"), 7)
        self.assertIsNone(items[0].get("weight_kg"))


class D4_ReferencesAndResends(CindyCatalogueBase):
    def test_such_cakes_requotes_the_last_request_with_the_new_count(self):
        self._post("Please send me a quotation for a 2 kg Vanilla cake")
        self.assertEqual(len(self._quotations()), 1)
        self._post("I wanted a quotation of 3 such cakes")
        quotations = self._quotations()
        self.assertEqual(len(quotations), 2, f"bot said {self._texts()}")
        lines = self._lines(quotations[1][0])
        self.assertEqual([int(line[1]) for line in lines], [3])
        self.assertIn("Vanilla", lines[0][0])

    def test_sending_the_quotation_again_issues_nothing_new(self):
        self._post("Please send me a quotation for a 2 kg Vanilla cake")
        self._post("Please send the quotation again")
        self.assertEqual(len(self._quotations()), 1)
        self.assertEqual(self._staff_unlisted(), 0)


class D5_UnlistedKeepsTheValidPart(CindyCatalogueBase):
    def test_unlisted_product_goes_to_staff_and_the_request_stays_open(self):
        self._post("Quote for 3 Vanilla cakes")
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["unicorn cake"]})]
        self._post("Also a quote for a unicorn cake")
        self.assertEqual(self._staff_unlisted(), 1)
        brief = self._brief()
        self.assertEqual(brief[0], "collecting")
        self.assertEqual([i.get("product_name") for i in self._items()], ["Vanilla"])
        self._post("2 kg")
        self.assertEqual(len(self._quotations()), 1)

    def test_unlisted_product_alone_is_still_handed_over(self):
        self.provider.replies = [json.dumps({"items": [], "unlisted": ["unicorn cake"]})]
        self._post("Can you quote a unicorn cake for me?")
        self.assertEqual(self._staff_unlisted(), 1)
        self.assertEqual(self._brief()[0], "handed_over")


class D9_MenuSaysTheRequestIsKept(CindyCatalogueBase):
    def test_menu_quotation_mid_request_reports_the_open_request(self):
        self._enable_quotations(**{"menu": {"enabled": True}})
        self._post("Please send me a quotation for 2 Black Forest cakes")
        self._post("Quotation")
        self.assertIn("still open", self._last_text())
        self.assertIn("Black Forest", self._last_text())


class PureHelpers(unittest.TestCase):
    def test_fragments(self):
        for text in ("Just one, get me a quote", "3 such cakes", "the same again please"):
            self.assertTrue(intent.is_fragment(text), text)
        for text in ("unicorn cake", "a galaxy one", "Carrot cake 2kg"):
            self.assertFalse(intent.is_fragment(text), text)

    def test_bare_number_and_referential_quantity(self):
        self.assertEqual(interpretation.bare_number("1"), 1)
        self.assertEqual(interpretation.bare_number("option 2"), 2)
        self.assertIsNone(interpretation.bare_number("1 kg"))
        self.assertIsNone(interpretation.bare_number("2 cakes please"))
        self.assertEqual(interpretation.referential_quantity("just one"), 1)
        self.assertEqual(interpretation.referential_quantity("3 such cakes"), 3)
        self.assertIsNone(interpretation.referential_quantity("just one kg"))
        self.assertIsNone(interpretation.referential_quantity("send it again"))

    def test_correction_wording(self):
        self.assertTrue(interpretation.is_correction("Actually make it Vanilla"))
        self.assertTrue(interpretation.is_correction("No, I meant chocolate"))
        self.assertFalse(interpretation.is_correction("2 vanilla cakes please"))

    def test_product_listing_stays_within_budget(self):
        products = [{"name": f"Product number {n:03d} with a long name"} for n in range(300)]
        listing = quotation_flow._product_listing(products)  # noqa: SLF001
        self.assertLessEqual(len(listing), quotation_flow._LISTING_BUDGET)  # noqa: SLF001
        self.assertRegex(listing, r"and \d+ more\.$")
        short = quotation_flow._product_listing([{"name": "A"}, {"name": "B"}])  # noqa: SLF001
        self.assertEqual(short, " We can quote for: A, B.")


if __name__ == "__main__":
    unittest.main()
