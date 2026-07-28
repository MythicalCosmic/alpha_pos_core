from base.repositories.base import BaseRepository


def test_base_repository_paginates_for_all_repository_subclasses():
    class Repository(BaseRepository):
        pass

    page, paginator = Repository.paginate(
        ['first', 'second', 'third', 'fourth', 'fifth'],
        page=2,
        per_page=2,
    )

    assert list(page) == ['third', 'fourth']
    assert paginator.count == 5
    assert paginator.num_pages == 3
