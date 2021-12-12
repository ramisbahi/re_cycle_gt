#Imports

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import optim

class ModelLSTM(nn.Module):
	def __init__(self, input_types, relation_types, model_dim, dropout = 0.5):
		super().__init__()

		self.word_types = input_types
		self.relation_types = relation_types
		self.dropout = dropout
		self.model_dim = model_dim

		self.emb = nn.Embedding(input_types, self.model_dim)
		self.lstm = nn.LSTM(self.model_dim, self.model_dim//2, batch_first=True, bidirectional=True, num_layers=2)
		self.relation_layer1 = nn.Linear(self.model_dim , self.model_dim)
		self.relation_layer2 = nn.Linear(self.model_dim , self.model_dim)
		self.drop = nn.Dropout(self.dropout)
		self.projection = nn.Linear(self.model_dim , self.model_dim)
		self.decoder = nn.Linear(self.model_dim , self.relation_types)
		self.layer_norm = nn.LayerNorm(self.model_dim)

		self.init_params()

	def init_params(self):
		nn.init.xavier_normal_(self.relation_layer1.weight.data)
		nn.init.xavier_normal_(self.relation_layer2.weight.data)
		nn.init.xavier_normal_(self.projection.weight.data)
		nn.init.xavier_normal_(self.decoder.weight.data)

		nn.init.constant_(self.relation_layer1.bias.data , 0)
		nn.init.constant_(self.relation_layer2.bias.data , 0)
		nn.init.constant_(self.projection.bias.data , 0)
		nn.init.constant_(self.decoder.bias.data , 0)

	#def forward(self, batch):
	def forward(self, sents, ent_inds):
		#sents = batch['text']
		sents, (c_0, h_0) = self.lstm(self.emb(sents))

		bs, _, hidden_dim = sents.shape
		
		max_ents = max([max([ent_ind[0] for ent_ind in batch_ent_inds])] for batch_ent_inds in ent_inds)[0].item() + 1
		
		
		cont_word_mask = sents.new_zeros(bs, max_ents)
		cont_word_embs = sents.new_zeros(bs, max_ents, hidden_dim)

		#for b, (sent,entind) in enumerate(zip(sents,batch['entity_inds'])):
		for b, (sent,entind) in enumerate(zip(sents, ent_inds)):
			for z in entind:
				if z[0] == -1:
					break
				else:
					wordemb = sent[z[1]:z[2]]
					mean_emb = torch.mean(wordemb, dim = 0)
					cont_word_embs[b, z[0]] = (cont_word_mask[b, z[0]]*cont_word_embs[b, z[0]] + mean_emb)/(cont_word_mask[b, z[0]] + 1)
					cont_word_mask[b, z[0]] += 1
			# for n_ent, wordemb in enumerate([sent[z[0]:z[1]] for z in entind]):
			# 	cont_word_embs[b, n_ent] = torch.mean(wordemb, dim = 0)
			# 	cont_word_mask[b, n_ent] = 1

		# bs x max_ents x model_dim
		cont_word_embs = self.layer_norm(cont_word_embs)
		cont_word_mask = torch.clamp(cont_word_mask, 0, 1)

		rel1 = self.relation_layer1(cont_word_embs)
		rel2 = self.relation_layer2(cont_word_embs)

		#bs x max_ents x max_ents x model_dim
		out = rel1.unsqueeze(1) + rel2.unsqueeze(2)

		out = self.drop(out)
		out = self.projection(out)
		out = self.decoder(out)

		out = out * cont_word_mask.view(bs,max_ents,1,1) * cont_word_mask.view(bs,1,max_ents,1)

		return torch.log_softmax(out, -1) # bs x max ents x max_ents x num_relations

class T2GModel():
	def __init__(self, vocab, device, model_dim):
		self.inp_types = len(vocab.entities.wordlist) + len(vocab.text.wordlist)
		self.rel_types = len(vocab.relations.wordlist)

		self.model = ModelLSTM(self.inp_types, self.rel_types, model_dim = model_dim).to(device)
		self.vocab = vocab
		self.device = device

	def eval(self):
		self.model.eval()
    
	def train(self):
		self.model.train()
	def t2g_preprocess(self, batch):
			""" 
				input: list of dictionaries in raw_json_format
				output: prepreprocessed dictionaries containing text, entity inds
			"""

			def entity2Indices(entity, mode = "T2G"):
				temp = torch.zeros(len(entity), dtype = torch.long)
				for ind, word in enumerate(entity):
					if word not in self.vocab.entities.word2idx:
						temp[ind] = self.vocab.entities.word2idx["<UNK>"]
					else:
						temp[ind] = self.vocab.entities.word2idx[word]
				return temp
					
			def text2Indices(text):
				temp = torch.zeros(len(text.split()) + 2, dtype=torch.long)
				temp[0] = self.vocab.text.word2idx["<SOS>"]
				for ind, word in enumerate(text.split()):
					if word not in self.vocab.text.word2idx:
						temp[ind + 1] = self.vocab.text.word2idx["<UNK>"]
					else:
						temp[ind + 1] = self.vocab.text.word2idx[word]
				temp[-1] = self.vocab.text.word2idx["<EOS>"]
				return temp
			


			def concatTextEntities(raw_json_sentence):
				sent = text2Indices(raw_json_sentence['text'])
				modified_input = torch.LongTensor([0])
				lbound = 0
				entity_locations = []
				additional_words = 0
				for index, value in enumerate(sent):
					if value.item() in self.vocab.entityindices:
						temp = entity2Indices(raw_json_sentence['entities'][self.vocab.entityindices[value.item()]])
						temp += len(self.vocab.text.wordlist)
						modified_input = torch.cat((modified_input, sent[lbound:index], temp), dim = 0)
						entity_locations.append([self.vocab.entityindices[value.item()], index + additional_words, index + additional_words + len(temp)])
						additional_words += len(temp) - 1
						lbound = index + 1
				modified_input = torch.cat((modified_input, sent[lbound:]), dim = 0)[1:]
				return modified_input, torch.LongTensor(entity_locations)

			maxlentext = 0
			maxents = 0
			temp_text = []
			temp_inds = []
			for raw_json_sentence in batch:
				(text, entity_inds) = concatTextEntities(raw_json_sentence)
				temp_inds.append(entity_inds)
				if len(entity_inds) > maxents:
					maxents = len(entity_inds)
				temp_text.append(text)
				if text.shape[0] > maxlentext:
					maxlentext = text.shape[0]
				
			final_text = torch.ones((len(batch), maxlentext), dtype = torch.long)*self.vocab.text.word2idx["<EMPTY>"]
			final_ents = torch.ones((len(batch), maxents, 3), dtype = torch.long)*-1

			for k in range(len(batch)):
				final_text[k][:len(temp_text[k])] = temp_text[k]
				final_ents[k][:len(temp_inds[k])] = temp_inds[k]
			
			return final_text, final_ents

	# input - texts with original entities taken out (list of dicts with text and entities)
	# output - batch of graphs (list of dicts with relations and entities)
	def predict(self, batch):
		preprocessed_text, preprocessed_inds = self.t2g_preprocess(batch)

		preds = self.model(preprocessed_text.to(self.device), preprocessed_inds.to(self.device))
		preds = torch.argmax(preds, -1)

		bs, ne, _ = preds.shape

		output = [] #list of dictionaries

		for b in range(bs):
			temp = {
				"relations": [],
				"entities": batch[b]['entities']
			}
			for i in range(0, ne):
				for j in range(i+1, ne):
					temp['relations'].append([temp['entities'][i], self.vocab.relations.wordlist[preds[b, i, j]], temp['entities'][j]])
			output.append(temp)
		return output
	
	

def train_model_supervised(model, num_relations, dataloader, learning_rate = 1e10, epochs = 30):
	"""
	"""

	# Create model
	optimzer = torch.optim.Adam(model.parameters(), lr=learning_rate)
	criterion = nn.NLLLoss()

	state_dict_clone = {k: v.clone() for k, v in model.state_dict().items()}
	for t in range(epochs):
		loss_this_epoch = 0.0
		for batch in tqdm.tqdm(range(len(dataloader))):
    
			log_probs = model(dataloader[batch])
			labels = dataloader[batch]['labels']	

			loss = criterion(log_probs.view(-1, num_relations), labels.view(-1))
			loss_this_epoch += loss.item()
			optimzer.zero_grad()
			loss.backward()
			# torch.nn.utils.clip_grad_norm_(
			#     [p for group in optimzer.param_groups for p in group['params']], CLIP)
			optimzer.step()

		# 	# load best parameters
		# curr_state_dict = encdec_model.state_dict()
		# for key in state_dict_clone.keys():
		# 	curr_state_dict[key].copy_(state_dict_clone[key])


