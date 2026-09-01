from __future__ import annotations

from unittest import TestCase

from process_simplification.production_exceptions.service import (
	_exception_history_pagination,
)


class TestProductionExceptionReviewUnit(TestCase):
	def test_history_pagination_is_bounded_and_clamps_page(self):
		self.assertEqual(
			_exception_history_pagination(page=9, page_length=500, total_count=205),
			{
				"page": 3,
				"page_length": 100,
				"total_count": 205,
				"total_pages": 3,
				"has_next": False,
				"has_prev": True,
			},
		)

	def test_empty_history_stays_on_first_page(self):
		self.assertEqual(
			_exception_history_pagination(page=4, page_length=20, total_count=0),
			{
				"page": 4,
				"page_length": 20,
				"total_count": 0,
				"total_pages": 0,
				"has_next": False,
				"has_prev": False,
			},
		)
