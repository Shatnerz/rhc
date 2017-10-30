from __future__ import absolute_import
import pytest
import weakref

from rhc.database.dao import DAO


class Parent(DAO):

    TABLE = 'parent'

    FIELDS = (
        'id',
        'foo',
        'bar',
    )

    DEFAULT = dict(
        foo=0,
    )

    CALCULATED_FIELDS = dict(
        foo_bar='%s.foo + %s.bar' % (TABLE, TABLE),
    )

    CHILDREN = dict(
        child='rhc.database.test.test_basic.Child',
    )


class Child(DAO):

    TABLE = 'child'

    FIELDS = (
        'id',
        'parent_id',
        'name',
    )

    FOREIGN = dict(
        parent='rhc.database.test.test_basic.Parent',
    )

    @classmethod
    def by_name(cls, name):
        return cls.query().where('name=%s').execute(name, one=True)


def test_save(db):
    t = Parent(foo=1, bar=2).save()
    assert t.id is not None
    assert t.foo == 1
    assert t.bar == 2


def test_load(db):
    t = Parent(foo=1, bar=2).save()
    tt = Parent.load(t.id)
    assert t.id == tt.id
    assert tt.foo == 1
    assert tt.bar == 2


def test_default(db):
    t = Parent(bar=1).save()
    assert t.foo == 0


def test_calculated(db):
    t = Parent(foo=1, bar=2).save()
    tt = Parent.load(t.id)
    assert tt.foo_bar == 3


@pytest.fixture
def data(db):
    p = Parent(foo=1, bar=2).save()
    Child(parent=p, name='fred').save()
    Child(parent=p, name='sally').save()


def test_foreign(data):
    c = Child.by_name('fred')
    assert c.parent.foo_bar == 3


def test_children(data):
    p = Parent.list()[0]
    c = p.children(Child)
    assert len(c) == 2


def test_children_by_property(data):
    p = Parent.list()[0]
    c = p.child
    assert len(c) == 2


def test_join(data):
    rs = Parent.query().join(Child).execute()
    assert len(rs) == 2
    names = [p.child.name for p in rs]
    assert len(names) == 2
    assert 'fred' in names
    assert 'sally' in names


def test_dao_cleanup_from_save(db):
    obj = Parent(foo=1, bar=2).save()
    ref = weakref.ref(obj)
    assert obj is ref()
    obj = 0  # dereference
    assert ref() is None


def test_dao_cleanup_from_load(db):
    # save object and remember id
    obj_id = Parent(foo=1, bar=2).save().id
    assert obj_id
    obj = Parent.load(obj_id)
    ref = weakref.ref(obj)
    assert ref() is obj
    obj = 0
    assert ref() is None
