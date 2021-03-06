import collections
from fnmatch import fnmatch

from elasticsearch.exceptions import NotFoundError, RequestError
from six import iteritems, add_metaclass

from .field import Field
from .mapping import Mapping
from .utils import ObjectBase, merge, DOC_META_FIELDS, META_FIELDS
from .search import Search
from .connections import connections
from .exceptions import ValidationException, IllegalOperation
from .index import Index, DEFAULT_INDEX


class MetaField(object):
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class DocumentMeta(type):
    def __new__(cls, name, bases, attrs):
        # DocumentMeta filters attrs in place
        attrs['_doc_type'] = DocumentOptions(name, bases, attrs)
        return super(DocumentMeta, cls).__new__(cls, name, bases, attrs)

class IndexMeta(DocumentMeta):
    def __new__(cls, name, bases, attrs):
        index_opts = attrs.pop('Index', None)
        new_cls = super(IndexMeta, cls).__new__(cls, name, bases, attrs)
        new_cls._index = cls.construct_index(index_opts, bases)
        new_cls._index.document(new_cls)
        return new_cls

    @classmethod
    def construct_index(cls, opts, bases):
        if opts is None:
            for b in bases:
                if getattr(b, '_index', DEFAULT_INDEX) is not DEFAULT_INDEX:
                    return b._index
            return DEFAULT_INDEX

        i = Index(
            getattr(opts, 'name', '*'),
            using=getattr(opts, 'using', 'default')
        )
        i.settings(**getattr(opts, 'settings', {}))
        i.aliases(**getattr(opts, 'aliases', {}))
        for a in getattr(opts, 'analyzers', ()):
            i.analyzer(a)
        return i


class DocumentOptions(object):
    def __init__(self, name, bases, attrs):
        meta = attrs.pop('Meta', None)

        # get doc_type name, if not defined use 'doc'
        doc_type = getattr(meta, 'doc_type', 'doc')

        # create the mapping instance
        self.mapping = getattr(meta, 'mapping', Mapping(doc_type))

        # register all declared fields into the mapping
        for name, value in list(iteritems(attrs)):
            if isinstance(value, Field):
                self.mapping.field(name, value)
                del attrs[name]

        # add all the mappings for meta fields
        for name in dir(meta):
            if isinstance(getattr(meta, name, None), MetaField):
                params = getattr(meta, name)
                self.mapping.meta(name, *params.args, **params.kwargs)

        # document inheritance - include the fields from parents' mappings
        for b in bases:
            if hasattr(b, '_doc_type') and hasattr(b._doc_type, 'mapping'):
                self.mapping.update(b._doc_type.mapping, update_only=True)

    @property
    def name(self):
        return self.mapping.properties.name


@add_metaclass(DocumentMeta)
class InnerDoc(ObjectBase):
    """
    Common class for inner documents like Object or Nested
    """
    @classmethod
    def from_es(cls, data, data_only=False):
        if data_only:
            data = {'_source': data}
        return super(InnerDoc, cls).from_es(data)

@add_metaclass(IndexMeta)
class Document(ObjectBase):
    """
    Model-like class for persisting documents in elasticsearch.
    """
    @classmethod
    def _matches(cls, hit):
        return fnmatch(hit.get('_index', ''), cls._index._name) \
            and cls._doc_type.name == hit.get('_type')

    @classmethod
    def _get_using(cls, using=None):
        return using or cls._index._using

    @classmethod
    def _get_connection(cls, using=None):
        return connections.get_connection(cls._get_using(using))

    @classmethod
    def _default_index(cls, index=None):
        return index or cls._index._name

    @classmethod
    def init(cls, index=None, using=None):
        """
        Create the index and populate the mappings in elasticsearch.
        """
        i = cls._index
        if index:
            i = i.clone(name=index)
        i.save(using=using)

    def _get_index(self, index=None, required=True):
        if index is None:
            index = getattr(self.meta, 'index', None)
        if index is None:
            index = getattr(self._index, '_name', None)
        if index is None and required:
            raise ValidationException('No index')
        if index and '*' in index:
            raise ValidationException('You cannot write to a wildcard index.')
        return index

    def __getattr__(self, name):
        if name.startswith('_') and name[1:] in META_FIELDS:
            return getattr(self.meta, name[1:])
        return super(Document, self).__getattr__(name)

    def __repr__(self):
        return '%s(%s)' % (
            self.__class__.__name__,
            ', '.join('%s=%r' % (key, getattr(self.meta, key)) for key in
                      ('index', 'doc_type', 'id') if key in self.meta)
        )

    def __setattr__(self, name, value):
        if name.startswith('_') and name[1:] in META_FIELDS:
            return setattr(self.meta, name[1:], value)
        return super(Document, self).__setattr__(name, value)

    @classmethod
    def search(cls, using=None, index=None):
        """
        Create an :class:`~elasticsearch_dsl.Search` instance that will search
        over this ``Document``.
        """
        return Search(
            using=cls._get_using(using),
            index=cls._default_index(index),
            doc_type=[cls]
        )

    @classmethod
    def get(cls, id, using=None, index=None, **kwargs):
        """
        Retrieve a single document from elasticsearch using it's ``id``.

        :arg id: ``id`` of the document to be retireved
        :arg index: elasticsearch index to use, if the ``Document`` is
            associated with an index this can be omitted.
        :arg using: connection alias to use, defaults to ``'default'``

        Any additional keyword arguments will be passed to
        ``Elasticsearch.get`` unchanged.
        """
        es = cls._get_connection(using)
        doc = es.get(
            index=cls._default_index(index),
            doc_type=cls._doc_type.name,
            id=id,
            **kwargs
        )
        if not doc.get('found', False):
            return None
        return cls.from_es(doc)

    @classmethod
    def mget(cls, docs, using=None, index=None, raise_on_error=True,
             missing='none', **kwargs):
        """
        Retrieve multiple document by their ``id``\s. Returns a list of instances
        in the same order as requested.

        :arg docs: list of ``id``\s of the documents to be retireved or a list
            of document specifications as per
            https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-multi-get.html
        :arg index: elasticsearch index to use, if the ``Document`` is
            associated with an index this can be omitted.
        :arg using: connection alias to use, defaults to ``'default'``
        :arg missing: what to do when one of the documents requested is not
            found. Valid options are ``'none'`` (use ``None``), ``'raise'`` (raise
            ``NotFoundError``) or ``'skip'`` (ignore the missing document).

        Any additional keyword arguments will be passed to
        ``Elasticsearch.mget`` unchanged.
        """
        if missing not in ('raise', 'skip', 'none'):
            raise ValueError("'missing' must be 'raise', 'skip', or 'none'.")
        es = cls._get_connection(using)
        body = {
            'docs': [
                doc if isinstance(doc, collections.Mapping) else {'_id': doc}
                for doc in docs
            ]
        }
        results = es.mget(
            body,
            index=cls._default_index(index),
            doc_type=cls._doc_type.name,
            **kwargs
        )

        objs, error_docs, missing_docs = [], [], []
        for doc in results['docs']:
            if doc.get('found'):
                if error_docs or missing_docs:
                    # We're going to raise an exception anyway, so avoid an
                    # expensive call to cls.from_es().
                    continue

                objs.append(cls.from_es(doc))

            elif doc.get('error'):
                if raise_on_error:
                    error_docs.append(doc)
                if missing == 'none':
                    objs.append(None)

            # The doc didn't cause an error, but the doc also wasn't found.
            elif missing == 'raise':
                missing_docs.append(doc)
            elif missing == 'none':
                objs.append(None)

        if error_docs:
            error_ids = [doc['_id'] for doc in error_docs]
            message = 'Required routing not provided for documents %s.'
            message %= ', '.join(error_ids)
            raise RequestError(400, message, error_docs)
        if missing_docs:
            missing_ids = [doc['_id'] for doc in missing_docs]
            message = 'Documents %s not found.' % ', '.join(missing_ids)
            raise NotFoundError(404, message, {'docs': missing_docs})
        return objs

    def delete(self, using=None, index=None, **kwargs):
        """
        Delete the instance in elasticsearch.

        :arg index: elasticsearch index to use, if the ``Document`` is
            associated with an index this can be omitted.
        :arg using: connection alias to use, defaults to ``'default'``

        Any additional keyword arguments will be passed to
        ``Elasticsearch.delete`` unchanged.
        """
        es = self._get_connection(using)
        # extract routing etc from meta
        doc_meta = dict(
            (k, self.meta[k])
            for k in DOC_META_FIELDS
            if k in self.meta
        )
        doc_meta.update(kwargs)
        es.delete(
            index=self._get_index(index),
            doc_type=self._doc_type.name,
            **doc_meta
        )

    def to_dict(self, include_meta=False, skip_empty=True):
        """
        Serialize the instance into a dictionary so that it can be saved in elasticsearch.

        :arg include_meta: if set to ``True`` will include all the metadata
            (``_index``, ``_type``, ``_id`` etc). Otherwise just the document's
            data is serialized. This is useful when passing multiple instances into
            ``elasticsearch.helpers.bulk``.
        :arg skip_empty: if set to ``False`` will cause empty values (``None``,
            ``[]``, ``{}``) to be left on the document. Those values will be
            stripped out otherwise as they make no difference in elasticsearch.
        """
        d = super(Document, self).to_dict(skip_empty=skip_empty)
        if not include_meta:
            return d

        meta = dict(
            ('_' + k, self.meta[k])
            for k in DOC_META_FIELDS
            if k in self.meta
        )

        # in case of to_dict include the index unlike save/update/delete
        index = self._get_index(required=False)
        if index is not None:
            meta['_index'] = index

        meta['_type'] = self._doc_type.name
        meta['_source'] = d
        return meta

    def update(self, using=None, index=None,  detect_noop=True,
               doc_as_upsert=False, refresh=False, **fields):
        """
        Partial update of the document, specify fields you wish to update and
        both the instance and the document in elasticsearch will be updated::

            doc = MyDocument(title='Document Title!')
            doc.save()
            doc.update(title='New Document Title!')

        :arg index: elasticsearch index to use, if the ``Document`` is
            associated with an index this can be omitted.
        :arg using: connection alias to use, defaults to ``'default'``

        Any additional keyword arguments will be passed to
        ``Elasticsearch.update`` unchanged.
        """
        if not fields:
            raise IllegalOperation('You cannot call update() without updating individual fields. '
                                   'If you wish to update the entire object use save().')

        es = self._get_connection(using)

        # update given fields locally
        merge(self, fields)

        # prepare data for ES
        values = self.to_dict()

        # if fields were given: partial update
        doc = dict(
            (k, values.get(k))
            for k in fields.keys()
        )

        # extract routing etc from meta
        doc_meta = dict(
            (k, self.meta[k])
            for k in DOC_META_FIELDS
            if k in self.meta
        )
        body = {
            'doc': doc,
            'doc_as_upsert': doc_as_upsert,
            'detect_noop': detect_noop,
        }

        meta = es.update(
            index=self._get_index(index),
            doc_type=self._doc_type.name,
            body=body,
            refresh=refresh,
            **doc_meta
        )
        # update meta information from ES
        for k in META_FIELDS:
            if '_' + k in meta:
                setattr(self.meta, k, meta['_' + k])

    def save(self, using=None, index=None, validate=True, **kwargs):
        """
        Save the document into elasticsearch. If the document doesn't exist it
        is created, it is overwritten otherwise. Returns ``True`` if this
        operations resulted in new document being created.

        :arg index: elasticsearch index to use, if the ``Document`` is
            associated with an index this can be omitted.
        :arg using: connection alias to use, defaults to ``'default'``
        :arg validate: set to ``False`` to skip validating the document

        Any additional keyword arguments will be passed to
        ``Elasticsearch.index`` unchanged.
        """
        if validate:
            self.full_clean()

        es = self._get_connection(using)
        # extract routing etc from meta
        doc_meta = dict(
            (k, self.meta[k])
            for k in DOC_META_FIELDS
            if k in self.meta
        )
        doc_meta.update(kwargs)
        meta = es.index(
            index=self._get_index(index),
            doc_type=self._doc_type.name,
            body=self.to_dict(),
            **doc_meta
        )
        # update meta information from ES
        for k in META_FIELDS:
            if '_' + k in meta:
                setattr(self.meta, k, meta['_' + k])

        # return True/False if the document has been created/updated
        return meta['result'] == 'created'

