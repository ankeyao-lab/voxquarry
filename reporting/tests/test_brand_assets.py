from __future__ import annotations

import unittest

from reporting.brand_assets import BRAND_ASSET_REGISTRY_VERSION, brand_asset


class BrandAssetTests(unittest.TestCase):
    def test_consumes_verified_chengmu_brand_profile_urls(self) -> None:
        expected_hosts = {
            "CRAFTSMAN": "www.craftsman.com",
            "DEWALT": "www.dewalt.com",
            "EGO": "egopowerplus.com",
            "Einhell": "www.einhell.com",
            "Flex": "www.flexpowertools.com",
            "Greenworks": "www.greenworkstools.com",
            "Milwaukee": "www.milwaukeetool.com",
            "Porter-Cable": "www.portercable.com",
            "RIDGID": "www.ridgid.com",
            "RYOBI": "cdn.shopify.com",
            "Shopvac": "cdn.shopify.com",
            "Vacmaster": "cdn11.bigcommerce.com",
            "Vacmaster Pro": "cdn11.bigcommerce.com",
        }

        for brand, host in expected_hosts.items():
            with self.subTest(brand=brand):
                asset = brand_asset(brand)
                self.assertEqual(asset["asset_version"], BRAND_ASSET_REGISTRY_VERSION)
                self.assertEqual(asset["logo_source"], "official_page_brand_asset")
                self.assertIn(host, asset["logo_url"])
                self.assertTrue(asset["official_website"].startswith("https://"))

    def test_matching_is_casefolded_without_claiming_brand_identity(self) -> None:
        self.assertEqual(
            brand_asset("  porter-CABLE  ")["logo_url"],
            brand_asset("Porter-Cable")["logo_url"],
        )
        self.assertEqual(
            brand_asset("vacmaster pro")["logo_url"],
            brand_asset("Vacmaster Pro")["logo_url"],
        )

    def test_unknown_and_generic_brands_fail_closed_to_wordmark(self) -> None:
        for brand in ("Generic", "Bucket Head", "", "Unknown Brand"):
            with self.subTest(brand=brand):
                asset = brand_asset(brand)
                self.assertEqual(asset["logo_url"], "")
                self.assertEqual(asset["logo_source"], "text_wordmark_fallback")

    def test_black_decker_keeps_existing_official_domain_fallback(self) -> None:
        asset = brand_asset("BLACK+DECKER")
        self.assertEqual(asset["logo_source"], "official_domain_favicon")
        self.assertEqual(asset["official_website"], "https://blackanddecker.com")
        self.assertEqual(asset["logo_url"], "https://blackanddecker.com/favicon.ico")


if __name__ == "__main__":
    unittest.main()
