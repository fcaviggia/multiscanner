'''
Storage module that will interact with elasticsearch.
'''
from datetime import datetime
from time import sleep
from uuid import uuid4
from elasticsearch import Elasticsearch, helpers, exceptions
from elasticsearch.exceptions import TransportError

import storage


METADATA_FIELDS = [
    'MD5',
    'SHA1',
    'SHA256',
    'ssdeep',
    'tags',
    'Metadata',
]


class ElasticSearchStorage(storage.Storage):
    '''
    Subclass of Storage.
    '''
    DEFAULTCONF = {
        'ENABLED': False,
        'host': 'localhost',
        'port': 9200,
        'index': 'multiscanner_reports',
        'doc_type': 'report',
    }

    def setup(self):
        self.host = self.config['host']
        self.port = self.config['port']
        self.index = self.config['index']
        self.doc_type = self.config['doc_type']
        self.es = Elasticsearch(
            host=self.host,
            port=self.port
        )
        # Create the index if it doesn't exist
        es_indices = self.es.indices
        if not es_indices.exists(self.index):
            es_indices.create(self.index)

        # Create parent-child mappings if don't exist yet
        mappings = es_indices.get_mapping(index=self.index)
        if self.doc_type not in mappings[self.index]['mappings'].keys():
            es_indices.put_mapping(index=self.index, doc_type=self.doc_type, body={
                '_parent': {
                    'type': 'sample'
                }
            })
        if 'note' not in mappings[self.index]['mappings'].keys():
            es_indices.put_mapping(index=self.index, doc_type='note', body={
                '_parent': {
                    'type': 'sample'
                },
                'properties': {
                    'timestamp': {
                        'type': 'date'
                    }
                }
            })

        # Create de-dot preprocessor if doesn't exist yet
        try:
            dedot = self.es.ingest.get_pipeline('dedot')
        except TransportError:
            dedot = False
        if not dedot:
            script = {
                "inline": """void dedot(def field) {
                        if (field != null && field instanceof HashMap) {
                            ArrayList replacelist = new ArrayList();
                            for (String key : field.keySet()) {
                                if (key.contains('.')) {
                                    replacelist.add(key)
                                }
                            }
                            for (String oldkey : replacelist) {
                                String newkey = /\\./.matcher(oldkey).replaceAll(\"_\");
                                field.put(newkey, field.get(oldkey));
                                field.remove(oldkey);
                            }
                            for (String k : field.keySet()) {
                                dedot(field.get(k));
                            }
                        }
                    }
                    dedot(ctx);"""
                }
            self.es.ingest.put_pipeline(id='dedot', body={
                'description': 'Replace dots in field names with underscores.',
                'processors': [
                    {
                        "script": script
                    }
                ]
            })

        return True

    def store(self, report):
        sample_id_list = []
        sample_list = []
        report_list = []

        for filename in report:
            report[filename]['filename'] = filename
            try:
                sample_id = report[filename]['SHA256']
            except KeyError:
                sample_id = uuid4()
            sample_id_list.append(sample_id)

            if 'SHA256' in report[filename]:
                report_id = report[filename]['SHA256']
            else:
                report_id = uuid4()

            # Store metadata with the sample, not the report
            sample = {'filename': filename, 'tags': []}
            for field in METADATA_FIELDS:
                if field in report[filename]:
                    if len(report[filename][field]) != 0:
                        sample[field] = report[filename][field]
                    del report[filename][field]


            # TODO: Use ES's autogenerated IDs instead of UUID; better tailored for parallelization
            sample_list.append(
                {
                    '_index': self.index,
                    '_type': 'sample',
                    '_id': sample_id,
                    '_source': sample,
                    'pipeline': 'dedot'
                }
            )
            report_list.append(
                {
                    '_index': self.index,
                    '_type': self.doc_type,
                    '_id': report_id,
                    '_parent': sample_id,
                    '_source': report[filename],
                    'pipeline': 'dedot'
                }
            )

        result = helpers.bulk(self.es, sample_list)
        result2 = helpers.bulk(self.es, report_list)
        return sample_id_list

    def get_report(self, sample_id, report_id):
        try:
            result_sample = self.es.get(
                index=self.index, doc_type='sample',
                id=sample_id
            )
            result_report = self.es.get(
                index=self.index, doc_type=self.doc_type,
                id=report_id, parent=sample_id
            )
            result = result_report['_source'].copy()
            result.update(result_sample['_source'])
            return result
        except Exception as e:
            print(e)
            return None

    def search(self, query_string):
        '''Run a Query String query and return a list of sample_ids associated
        with the matches. Run the query against all document types.
        '''
        query = {"query": {"query_string": {"query": query_string}}}
        result = helpers.scan(
            self.es, query=query, index=self.index
        )

        matches = []
        for r in result:
            if r['_type'] == 'sample':
                field = '_id'
            else:
                field = '_parent'
            matches.append(r[field])
        return tuple(set(matches))

    def add_tag(self, sample_id, tag):
        script = {
            "script": {
                "inline": "ctx._source.tags.add(params.tag)",
                "lang": "painless",
                "params": {
                    "tag": tag
                }
            }
        }

        try:
            result = self.es.update(
                index=self.index, doc_type='sample',
                id=sample_id, body=script
            )
            return result
        except:
            return None

    def remove_tag(self, sample_id, tag):
        script = {
            "script": {
                "inline": """def i = ctx._source.tags.indexOf(params.tag);
                    if (i > -1) { ctx._source.tags.remove(i); }""",
                "lang": "painless",
                "params": {
                    "tag": tag
                }
            }
        }

        try:
            result = self.es.update(
                index=self.index, doc_type='sample',
                id=sample_id, body=script
            )
            return result
        except:
            return None

    def get_tags(self):
        script = {
            "query": {
                "match_all": {}
            },
            "aggs": {
                "tags_agg": {
                    "terms": {
                        "field": "tags.keyword"
                    }
                }
            }
        }

        result = self.es.search(
            index=self.index, doc_type='sample', body=script
        )
        return result

    def get_notes(self, sample_id, search_after=None):
        query = {
            "query": {
                "has_parent": {
                    "type": "sample",
                    "query": {
                        "match": {
                            "_id": sample_id
                        }
                    }
                }
            },
            "sort": [
                {
                    "timestamp": {
                        "order": "asc"
                    }
                },
                {
                    "_uid": {
                        "order": "desc"
                    }
                }
            ]
        }
        if search_after:
            query['search_after'] = search_after

        result = self.es.search(
            index=self.index, doc_type='note', body=query
        )
        return result

    def get_note(self, sample_id, note_id):
        try:
            result = self.es.get(
                index=self.index, doc_type='note',
                id=note_id, parent=sample_id
            )
            return result
        except:
            return None

    def add_note(self, sample_id, data):
        data['timestamp'] = datetime.now().isoformat()
        result = self.es.create(
            index=self.index, doc_type='note', id=uuid4(), body=data,
            parent=sample_id
        )
        if result['result'] == 'created':
            return self.get_note(sample_id, result['_id'])
        return result

    def edit_note(self, sample_id, note_id, text):
        partial_doc = {
            "doc": {
                "text": text
            }
        }
        result = self.es.update(
            index=self.index, doc_type='note', id=note_id,
            body=partial_doc, parent=sample_id
        )
        if result['result'] == 'created':
            return self.get_note(sample_id, result['_id'])
        return result

    def delete_note(self, sample_id, note_id):
        result = self.es.delete(
            index=self.index, doc_type='note', id=note_id,
            parent=sample_id
        )
        return result

    def delete(self, report_id):
        try:
            self.es.delete(
                index=self.index, doc_type=self.doc_type,
                id=report_id
            )
            return True
        except:
            return False

    def teardown(self):
        pass
