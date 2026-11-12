from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from registry.catalog import OWNERSHIPS, CATEGORIES
from registry.values import object_fields, identifier, integer, fingerprint, DataError


@dataclass(frozen=True)
class Definition:
    id: str
    version: int
    numerator: tuple
    denominator: tuple
    ownerships: tuple
    target_basis_points: int
    small_denominator: int

    def export(self):
        return {
            'id': self.id,
            'version': self.version,
            'numerator': list(self.numerator),
            'denominator': list(self.denominator),
            'ownerships': list(self.ownerships),
            'target_basis_points': self.target_basis_points,
            'small_denominator': self.small_denominator,
        }

    @property
    def digest(self):
        return fingerprint(self.export())


def parse_definition(value):
    object_fields(value, ('id', 'version', 'numerator', 'denominator', 'ownerships', 'target_basis_points', 'small_denominator'))
    identifier(value['id'], 'definition.id')
    integer(value['version'], 'definition.version', 1)
    integer(value['target_basis_points'], 'definition.target_basis_points', 0, 10000)
    integer(value['small_denominator'], 'definition.small_denominator', 0)
    for key, allowed in (('numerator', CATEGORIES), ('denominator', CATEGORIES), ('ownerships', OWNERSHIPS)):
        values = value[key]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
            raise DataError('definition.' + key, 'expected nonempty string list')
        if len(values) != len(set(values)) or set(values) - allowed:
            raise DataError('definition.' + key, 'duplicate or unknown member')
    if not set(value['numerator']) <= set(value['denominator']):
        raise DataError('definition', 'numerator must be subset of denominator')
    return Definition(value['id'], value['version'], tuple(sorted(value['numerator'])), tuple(sorted(value['denominator'])), tuple(sorted(value['ownerships'])), value['target_basis_points'], value['small_denominator'])


def nursing_definition():
    return parse_definition({
        'id': 'licensed-nursing-share',
        'version': 1,
        'numerator': ['nursing'],
        'denominator': sorted(CATEGORIES),
        'ownerships': sorted(OWNERSHIPS),
        'target_basis_points': 7300,
        'small_denominator': 5,
    })
