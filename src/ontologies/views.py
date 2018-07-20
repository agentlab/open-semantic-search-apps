from django.shortcuts import render
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.template import RequestContext
from django.views import generic
from django.forms import ModelForm
from django.http import HttpResponseRedirect
from django.contrib import messages

from ontologies.models import Ontologies

import thesaurus.views
from thesaurus.models import Facet
from thesaurus.models import Concept

from opensemanticetl.enhance_extract_text_tika_server import enhance_extract_text_tika_server
import opensemanticetl.etl_sparql
import opensemanticetl.export_solr

from solr_ontology_tagger import OntologyTagger
from dictionary.manager import Dictionary_Manager
from entity_import.entity_import_list import Entity_Importer_List

import os
import os.path
import tempfile

from urllib.request import urlretrieve
from urllib.request import urlopen


class OntologiesForm(ModelForm):

	class Meta:
		model = Ontologies
		fields = '__all__'

class IndexView(generic.ListView):
	model = Ontologies

class DetailView(generic.DetailView):
	model = Ontologies

class CreateView(generic.CreateView):
	model = Ontologies

class UpdateView(generic.UpdateView):
	model = Ontologies


#
# New/additional ontology, so rewrite/update named entity dictionaries and facet configs
#

def create_ontology(request):

	if request.method == 'POST':

		form = OntologiesForm(request.POST, request.FILES)

		if form.is_valid():
			ontology = form.save()

			write_named_entities_config()

			return HttpResponseRedirect( reverse('ontologies:detail', args=[ontology.pk]) ) # Redirect after POST

	else:
		form = OntologiesForm()

	return render(request, 'ontologies/ontologies_form.html', 
			{'form': form,	} )
	

#
# Updated an ontology, so rewrite/update named entity dictionaries and facet configs
#

def update_ontology(request, pk):

	ontology = Ontologies.objects.get(pk=pk)
	
	if request.POST:
		
		form = OntologiesForm(request.POST, request.FILES, instance=ontology)
		
		if form.is_valid():
			form.save()

			write_named_entities_config()

			return HttpResponseRedirect( reverse('ontologies:detail', args=[pk])) # Redirect after POST
		
			pass
	else:
		form = OntologiesForm(instance=ontology)

	return render(request, 'ontologies/ontologies_form.html', 
			{'form': form, 'ontology': ontology } )


#
# Request to start to tag all untagged documents, that were indexed before with entities / labels of all ontologies
#

def apply_ontologies(request):

	count = 0

	for ontology in Ontologies.objects.all():
		
		count += 1

		tag_by_ontology(ontology)

	return render(request, 'ontologies/ontologies_apply_ontologies.html', {'count': count,})


#
# Request to tag all untagged documents, that were indexed before with entities / labels of an ontology
#

def apply_ontology(request, pk):


	ontology = Ontologies.objects.get(pk=pk)
	
	count = tag_by_ontology(ontology)
	
	return render(request, 'ontologies/ontologies_apply_ontology.html', {'id': pk, 'count': count,})


#
# Get local file(name) of the ontology
#

# Therefore download to tempfile or reference to local file

def get_ontology_file(ontology):

	# if local file, file no temp file
	is_tempfile = False

	if ontology.file:

		filename = ontology.file.path

	elif ontology.sparql_endpoint:
		
		is_tempfile = True

		if ontology.sparql_query.startswith("SELECT "):
			filename = opensemanticetl.etl_sparql.sparql_select_to_list_file(ontology.sparql_endpoint, ontology.sparql_query)
		else:
			filename = opensemanticetl.etl_sparql.download_rdf_from_sparql_endpoint(ontology.sparql_endpoint, ontology.sparql_query)

	elif ontology.uri.startswith('file://'):

		# filename is file URI without protocol prefix file://
		filename = ontology.uri[len('file://'):]

	else:
		# Download url to an tempfile
		is_tempfile = True
		filename, headers = urlretrieve(ontology.uri)

	print (filename)

	return is_tempfile, filename


#
#  Tag indexed documents containing this entry or label(s) of every entry/entity in ontology or list
#

def tag_by_ontology(ontology):

	# get the ontology file
	is_tempfile, filename = get_ontology_file(ontology)
	
	facet =  get_facetname(ontology)

	contenttype, encoding = get_contenttype_and_encoding(filename)
	
	if contenttype == 'application/rdf+xml':

		ontology_tagger = OntologyTagger()

		#load graph from RDF file
		ontology_tagger.parse(filename)

		# tag the documents on Solr server with all matching entities of the ontology	
		ontology_tagger.tag = True
		ontology_tagger.apply(target_facet=facet)

	elif contenttype.startswith('text/plain'):
		tag_by_list(filename=filename, field=facet, encoding=encoding)
	
	else:
		# create empty list so configs of field in schema.xml pointing to this file or in facet config of UI will not break
		print ( "Unknown format {}".format(contenttype) )

	#
	# Delete if downloaded ontology by URL to tempfile
	#
	if is_tempfile:
		os.remove(filename)


#
# Tag documents by dictionary in plaintext format
#

# Therefore search for each line of plaintextfile and add/tag the entity/entry/line to the facet/field of all documents matching this entry

def tag_by_list(filename, field, encoding='utf-8'):

	# open and read plaintext file line for line

	file = open(filename, encoding=encoding)

	for line in file:
		
		value = line.strip()
	
		if value:

			# mask line/entry for search query			
			searchquery = "\"" + opensemanticetl.export_solr.solr_mask(value) + "\""
		
			solr = opensemanticetl.export_solr.export_solr()
			
			# tag the field/facet of all ducuments matching this query by value of entry
			count = solr.update_by_query( searchquery, field=field, value=value)

	file.close()


#
# Append entries/lines from an list/dictionary to another
#

def append_from_txtfile(sourcefilename, encoding='utf-8', wordlist_configfilename=None):
	
	appended_words = []

	source = open(sourcefilename, 'r', encoding=encoding)

	wordlist_file = open(wordlist_configfilename, 'a', encoding="UTF-8")

	for line in source:
		if line:

			if wordlist_configfilename:
				# Append single words of concept labels to wordlist for OCR word dictionary

				words = line.split()
				for word in words:
					word = word.strip("(),")
					if word:
						if word not in appended_words:
							appended_words.append(word)
							appended_words.append(word.upper())
							wordlist_file.write(word + "\n")
							wordlist_file.write(word.upper() + "\n")


	source.close()

	wordlist_file.close()


#
# Write facets config for search UI
#

# Collect all used facets so they can be displayed for search UI config

def write_facet_config(automatch_facets=[]):
	# Todo: graph with labels or JSON instead of PHP config
	
	configfilename_php = '/etc/solr-php-ui/config.facets.php'
	configfilename_python = '/etc/opensemanticsearch/facets'
	
	configfile_php = open(configfilename_php, 'w', encoding="utf-8")
	configfile_python = open(configfilename_python, 'w', encoding="utf-8")

	configfile_php.write("<?php\n// do not config here, this config file will be overwritten by Thesaurus and Ontologies Manager\n")

	configfile_python.write("# do not config here, this config file will be overwritten by Thesaurus and Ontologies Manager\n")
	configfile_python.write("config['facets']={}\n")

	facets_done=[]

	# add facets of named entities
	for facet in Facet.objects.filter(enabled=True).order_by('facet_order'):
		facets_done.append(facet.facet)
		
		configfile_php.write("\n$cfg['facets']['{}'] = array ('label'=>'{}', 'facet_limit'=>'{}', 'snippets_limit'=>'{}', 'graph_limit'=>'{}'" . format( facet.facet, facet.label, facet.facet_limit, facet.snippets_limit, facet.graph_limit))

		if facet.snippets_enabled:
			configfile_php.write(",'snippets_enabled'=>true")
		else:
			configfile_php.write(",'snippets_enabled'=>false")

		if facet.graph_enabled:
			configfile_php.write(",'graph_enabled'=>true")
		else:
			configfile_php.write(",'graph_enabled'=>false")

		configfile_php.write(");\n")

		configfile_python.write( "config['facets']['{}'] = ". format(facet.facet) )
		configfile_python.write( "{" )
		configfile_python.write( "'label': '{}', 'uri': '{}', 'facet_limit': '{}', 'snippets_limit': '{}'," . format( facet.label, facet.uri, facet.facet_limit, facet.snippets_limit) )

		if facet.facet in automatch_facets:
			configfile_python.write( "'dictionary': '{}'".format('dictionary_matcher_' + facet.facet) )

		configfile_python.write("}\n")
			
	# add facets of ontolgoies
	for ontology in Ontologies.objects.all():

		facet = get_facetname(ontology)

		if facet not in facets_done:
		
			facets_done.append(facet)
			
			configfile_php.write( "\n$cfg['facets']['{}'] = array ('label'=>'{}');\n".format( facet, ontology ) )

			configfile_python.write( "config['facets']['{}'] = ". format(facet) )
			configfile_python.write( "{" )
			configfile_python.write( "'label': '{}', 'uri': '{}', 'facet_limit': '{}', 'snippets_limit': '{}'," . format( ontology.title, ontology.uri, 0, 0) )

			if facet in automatch_facets:
				configfile_python.write( " 'dictionary': '{}'".format('dictionary_matcher_' + facet) )

			configfile_python.write("}\n")

	configfile_php.write('?>')
	
	configfile_php.close()
	configfile_python.close()


#
# Clean facetname and listfilename
# so it can be used in XML configs within quotes
#

def clean_facetname(facet):
	
	facet = facet.replace("\"", "")
	facet = facet.replace("\'", "")
	facet = facet.replace("\\", "")
	facet = facet.replace("/", "")
	facet = facet.replace("?", "")
	facet = facet.replace("&", "_")
	facet = facet.replace("$", "")
	facet = facet.replace("<", "")
	facet = facet.replace(">", "")
	facet = facet.replace("|", "_")
	facet = facet.replace(":", "_")
	facet = facet.replace(".", "_")
	facet = facet.replace(",", "_")
	facet = facet.replace(" ", "_")

	facet=facet.strip()
	
	return facet


#
# Mask facet name of the ontology
#

def get_facetname(ontology):

	if ontology.facet:
		facet = ontology.facet.facet
	else:
		if ontology.title:
			facet = ontology.title
		elif ontology.file.name:
			# filename without path
			facet = os.path.basename(ontology.file.name)
		# not every uri can be used as filename, so don't use it, take better id
		#elif ontology.uri:
		#	facet = ontology.uri
		else:
			facet = "ontology_{}".format(ontology.id)

		# remove special chars and add type suffix
		facet = clean_facetname(facet)
		facet = facet+'_ss'

	return facet


#
# Analyze contenttype (plaintextlist or ontology?) and encoding
#

def get_contenttype_and_encoding(filename):

		# use Tika and data enrichment/data analysis functions from ETL
		tika = enhance_extract_text_tika_server()
		parameters = {}
		parameters['filename'] = filename
		parameters, data = tika.process(parameters=parameters, data = {})
		contenttype = data['content_type_ss']

		# get charset if plain text file to extract with right charset
		if 'encoding_s' in data:
			encoding = data['encoding_s']
		else:
			encoding = 'utf-8'

		return contenttype, encoding


#
# Write entities configs
#

def	write_named_entities_config():

	dictionary_manager = Dictionary_Manager()

	wordlist_configfilename = "/etc/opensemanticsearch/ocr/dictionary.txt"
	
	tmp_wordlist_configfilename = dictionary_manager.solr_dictionary_config_path + os.path.sep + 'tmp_ocr_dictionary.txt'

	facets = []

	# create named entities configs for all ontologies
	for ontology in Ontologies.objects.all():
		
		print ("Importing Ontology or List {} (ID: {})".format( ontology, ontology.id ) )
	
		# Download, if URI
		is_tempfile, filename = get_ontology_file(ontology)
		
		facet = get_facetname(ontology)
	
		# analyse content type & encoding
		contenttype, encoding = get_contenttype_and_encoding(filename)
		print ( "Detected content type: {}".format(contenttype) )
		print ( "Detected encoding: {}".format(encoding) )


		# file to export all labels
		tmplistfilename = dictionary_manager.solr_dictionary_config_path + os.path.sep + 'tmp_' + facet + '.txt'

		#
		# export entries to listfiles
		#
		
		if contenttype=='application/rdf+xml':

			#
			# write labels, words and synonyms config files
			#

			ontology_tagger = OntologyTagger()

			# load graph from RDF file
			ontology_tagger.parse(filename)
			
			# add the labels to entities index for normalization and entity linking
			ontology_tagger.solr_entities = os.getenv('ONTO_TAGGER_SOLR_ENTITIES_URL', default='http://localhost:8983/solr/')
			ontology_tagger.solr_core_entities = os.getenv('ONTO_TAGGER_SOLR_CORE_ENTITIES', default='opensemanticsearch-entities')
			
			# append synonyms to Solr managed synonyms resource "skos"
			ontology_tagger.solr = os.getenv('ONTO_TAGGER_SOLR_URL', default='http://localhost:8983/solr/')
			ontology_tagger.solr_core = os.getenv('ONTO_TAGGER_SOLR_CORE', default='opensemanticsearch')
			ontology_tagger.synonyms_resourceid = os.getenv('ONTO_TAGGER_SYN_RESOURCEID', default='skos')

			# append single words of concept labels to wordlist for OCR word dictionary
			ontology_tagger.wordlist_configfile = tmp_wordlist_configfilename

			# append all labels to the facets labels list
			ontology_tagger.labels_configfile = tmplistfilename
			
			# write synonyms config file
			ontology_tagger.apply(target_facet=facet)

			
		elif contenttype.startswith('text/plain'):
			append_from_txtfile(sourcefilename=filename, encoding=encoding, wordlist_configfilename=tmp_wordlist_configfilename)
			importer = Entity_Importer_List()
			importer.import_entities(filename=filename, types=[facet], dictionary=facet, facet_dictionary_is_tempfile=True, encoding=encoding)

		else:
			print ( "Unknown format {}".format(contenttype) )
		
		# remember each new facet for which there a list has been created so we can later write all this facets to schema.xml config part
		if not facet in facets:
			facets.append(facet)
		
		# Delete if downloaded ontology by URL to tempfile
		if is_tempfile:
			os.remove(filename)

	# Write thesaurus entries to facet entities list(s) / dictionaries, entities index and synonyms
	thesaurus_facets = thesaurus.views.export_entities(wordlist_configfilename=tmp_wordlist_configfilename, facet_dictionary_is_tempfile=True)

	# add facets used in thesaurus but not yet in an ontology to facet config
	for thesaurus_facet in thesaurus_facets:
		if not thesaurus_facet in facets:
			facets.append(thesaurus_facet)

	# Move new and complete facet file to destination
	for facet in facets:
		
		tmplistfilename = dictionary_manager.solr_dictionary_config_path + os.path.sep + 'tmp_' + facet + '.txt'
		listfilename = dictionary_manager.solr_dictionary_config_path + os.path.sep + facet + '.txt'
		os.rename(tmplistfilename, listfilename)

	# Move temp synonyms and OCR words config file to destination
	if os.path.isfile(tmp_wordlist_configfilename):
		os.rename(tmp_wordlist_configfilename, wordlist_configfilename)
	
	# Add facet dictionaries to Open Semantic Entity Search API config
	for facet in facets:

		dictionary_manager.create_dictionary(facet)

	# Create config for UI
	write_facet_config(automatch_facets=facets)
	
	# Reload/restart Solr core / schema / config to apply changed configs
	# so added config files / ontolgies / facets / new dictionary entries will be considered by analyzing/indexing new documents
	# Todo: Use the Solr URI from config
	solr = os.getenv('ONTO_TAGGER_SOLR_URL', default='http://localhost:8983/solr/')
	solr_core = os.getenv('ONTO_TAGGER_SOLR_CORE', default='opensemanticsearch')
	urlopen(solr + 'admin/cores?action=RELOAD&core=' + solr_core)
	
	solr_entities = os.getenv('ONTO_TAGGER_SOLR_ENTITIES_URL', default='http://localhost:8983/solr/')
	solr_core_entities = os.getenv('ONTO_TAGGER_SOLR_CORE_ENTITIES', default='opensemanticsearch-entities')
	urlopen(solr_entities + 'admin/cores?action=RELOAD&core=' + solr_core_entities)
