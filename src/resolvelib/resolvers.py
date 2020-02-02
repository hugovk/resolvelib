import collections

from .providers import AbstractResolver
from .structs import DirectedGraph


RequirementInformation = collections.namedtuple(
    "RequirementInformation", ["requirement", "parent"]
)


class ResolverException(Exception):
    """A base class for all exceptions raised by this module.

    Exceptions derived by this class should all be handled in this module. Any
    bubbling pass the resolver should be treated as a bug.
    """


class RequirementsConflicted(ResolverException):
    def __init__(self, criterion):
        super(RequirementsConflicted, self).__init__()
        self.criterion = criterion


class Criterion(object):
    """Representation of possible resolution results of a package.

    This holds three attributes:

    * `information` is a collection of `RequirementInformation` pairs.
      Each pair is a requirement contributing to this criterion, and the
      candidate that provides the requirement.
    * `incompatibilities` is a collection of all known not-to-work candidates
      to exclude from consideration.
    * `candidates` is a collection containing all possible candidates deducted
      from the union of contributing requirements and known incompatibilities.
      It should never be empty.

    .. note::
        This class is intended to be externally immutable. **Do not** mutate
        any of its attribute containers.
    """

    def __init__(self, candidates, information, incompatibilities):
        self.candidates = candidates
        self.information = information
        self.incompatibilities = incompatibilities

    @classmethod
    def from_requirement(cls, provider, requirement, parent):
        """Build an instance from a requirement.
        """
        return cls(
            candidates=provider.find_matches(requirement),
            information=[RequirementInformation(requirement, parent)],
            incompatibilities=[],
        )

    def iter_requirement(self):
        return (i.requirement for i in self.information)

    def iter_parent(self):
        return (i.parent for i in self.information)

    def merged_with(self, provider, requirement, parent):
        """Build a new instance from this and a new requirement.
        """
        infos = list(self.information)
        infos.append(RequirementInformation(requirement, parent))
        candidates = [
            c
            for c in self.candidates
            if provider.is_satisfied_by(requirement, c)
        ]
        if not candidates:
            raise RequirementsConflicted(self)
        return type(self)(candidates, infos, list(self.incompatibilities))

    def excluded_of(self, candidate):
        """Build a new instance from this, but excluding specified candidate.
        """
        incompats = list(self.incompatibilities)
        incompats.append(candidate)
        candidates = [c for c in self.candidates if c != candidate]
        if not candidates:
            raise RequirementsConflicted(self)
        return type(self)(candidates, list(self.information), incompats)


class ResolutionError(ResolverException):
    pass


class ResolutionImpossible(ResolutionError):
    def __init__(self, requirements):
        super(ResolutionImpossible, self).__init__(requirements)
        self.requirements = requirements


class ResolutionTooDeep(ResolutionError):
    def __init__(self, round_count):
        super(ResolutionTooDeep, self).__init__(round_count)
        self.round_count = round_count


# Resolution state in a round.
State = collections.namedtuple("State", "mapping criteria")


class Resolution(object):
    """Stateful resolution object.

    This is designed as a one-off object that holds information to kick start
    the resolution process, and holds the results afterwards.
    """

    def __init__(self, provider, reporter):
        self._p = provider
        self._r = reporter
        self._states = []

    @property
    def state(self):
        try:
            return self._states[-1]
        except IndexError:
            raise AttributeError("state")

    def _push_new_state(self):
        """Push a new state into history.

        This new state will be used to hold resolution results of the next
        coming round.
        """
        try:
            base = self._states[-1]
        except IndexError:
            state = State(mapping=collections.OrderedDict(), criteria={})
        else:
            state = State(
                mapping=base.mapping.copy(), criteria=base.criteria.copy(),
            )
        self._states.append(state)

    def _contribute_to_criteria(self, name, requirement, parent):
        try:
            crit = self.state.criteria[name]
        except KeyError:
            crit = Criterion.from_requirement(self._p, requirement, parent)
        else:
            crit = crit.merged_with(self._p, requirement, parent)
        self.state.criteria[name] = crit

    def _get_criterion_item_preference(self, item):
        name, criterion = item
        try:
            pinned = self.state.mapping[name]
        except KeyError:
            pinned = None
        return self._p.get_preference(
            pinned, criterion.candidates, criterion.information,
        )

    def _is_current_pin_satisfying(self, name, criterion):
        try:
            current_pin = self.state.mapping[name]
        except KeyError:
            return False
        return all(
            self._p.is_satisfied_by(r, current_pin)
            for r in criterion.iter_requirement()
        )

    def _check_pinnability(self, candidate):
        backup = self.state.criteria.copy()
        try:
            for subdep in self._p.get_dependencies(candidate):
                key = self._p.identify(subdep)
                self._contribute_to_criteria(key, subdep, parent=candidate)
        except RequirementsConflicted:
            criteria = self.state.criteria
            criteria.clear()
            criteria.update(backup)
            return False
        return True

    def _pin_criterion(self, name, criterion):
        for candidate in reversed(criterion.candidates):
            if not self._check_pinnability(candidate):
                continue
            self.state.mapping.pop(name, None)
            self.state.mapping[name] = candidate
            return True

        # All candidates tried, nothing works. This criterion is a dead
        # end, signal for backtracking.
        return False

    def _backtrack_to_last_workable_state(self, criterion):
        while criterion:
            del self._states[-1]

            # Nowhere to go, this is unsolvable.
            if not self._states:
                requirements = list(criterion.iter_requirement())
                raise ResolutionImpossible(requirements)

            name, candidate = self.state.mapping.popitem()
            try:
                criterion = self.state.criteria[name].excluded_of(candidate)
            except RequirementsConflicted:
                continue
            self.state.criteria[name] = criterion
            break

    def resolve(self, requirements, max_rounds):
        if self._states:
            raise RuntimeError("already resolved")

        self._push_new_state()
        for requirement in requirements:
            try:
                name = self._p.identify(requirement)
                self._contribute_to_criteria(name, requirement, parent=None)
            except RequirementsConflicted as e:
                # If initial requirements conflict, nothing would ever work.
                raise ResolutionImpossible(e.requirements + [requirement])

        self._r.starting()

        for round_index in range(max_rounds):
            self._r.starting_round(round_index)

            self._push_new_state()
            curr = self.state

            criterion_items = [
                item
                for item in self.state.criteria.items()
                if not self._is_current_pin_satisfying(*item)
            ]

            # All criteria are accounted for. Nothing more to pin, we are done!
            if not criterion_items:
                del self._states[-1]
                self._r.ending(curr)
                return

            # Choose the most preferred unpinned criterion to try.
            name, criterion = min(
                criterion_items, key=self._get_criterion_item_preference,
            )
            success = self._pin_criterion(name, criterion)

            if not success:
                self._backtrack_to_last_workable_state(criterion)
            self._r.ending_round(round_index, curr)

        raise ResolutionTooDeep(max_rounds)


def _has_route_to_root(criteria, key, all_keys, connected):
    if key in connected:
        return True
    if key not in criteria:
        return False
    for p in criteria[key].iter_parent():
        try:
            pkey = all_keys[id(p)]
        except KeyError:
            continue
        if pkey in connected:
            connected.add(key)
            return True
        if _has_route_to_root(criteria, pkey, all_keys, connected):
            connected.add(key)
            return True
    return False


Result = collections.namedtuple("Result", "mapping graph criteria")


def _build_result(state):
    mapping = state.mapping
    all_keys = {id(v): k for k, v in mapping.items()}
    all_keys[id(None)] = None

    graph = DirectedGraph()
    graph.add(None)  # Sentinel as root dependencies' parent.

    connected = {None}
    for key, criterion in state.criteria.items():
        if not _has_route_to_root(state.criteria, key, all_keys, connected):
            continue
        if key not in graph:
            graph.add(key)
        for p in criterion.iter_parent():
            try:
                pkey = all_keys[id(p)]
            except KeyError:
                continue
            if pkey not in graph:
                graph.add(pkey)
            graph.connect(pkey, key)

    return Result(
        mapping={k: v for k, v in mapping.items() if k in connected},
        graph=graph,
        criteria=state.criteria,
    )


class Resolver(AbstractResolver):
    """The thing that performs the actual resolution work.
    """

    base_exception = ResolverException

    def resolve(self, requirements, max_rounds=100):
        """Take a collection of constraints, spit out the resolution result.

        The return value is a representation to the final resolution result. It
        is a tuple subclass with three public members:

        * `mapping`: A dict of resolved candidates. Each key is an identifier
            of a requirement (as returned by the provider's `identify` method),
            and the value is the resolved candidate.
        * `graph`: A `DirectedGraph` instance representing the dependency tree.
            The vertices are keys of `mapping`, and each edge represents *why*
            a particular package is included. A special vertex `None` is
            included to represent parents of user-supplied requirements.
        * `criteria`: A dict of "criteria" that hold detailed information on
            how edges in the graph are derived. Each key is an identifier of a
            vertex, and the value is a `Criterion` instance.

        The following exceptions may be raised if a resolution cannot be found:

        * `ResolutionImpossible`: A resolution cannot be found for the given
            combination of requirements.
        * `ResolutionTooDeep`: The dependency tree is too deeply nested and
            the resolver gave up. This is usually caused by a circular
            dependency, but you can try to resolve this by increasing the
            `max_rounds` argument.
        """
        resolution = Resolution(self.provider, self.reporter)
        resolution.resolve(requirements, max_rounds=max_rounds)
        return _build_result(resolution.state)
