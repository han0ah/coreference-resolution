# TODO:
# Convert to sentence LSTM

# Eval
# Early stopping
# Write back to CoNLL format
# No more slicing (?)
# Batching documents

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchtext.vocab import Vectors

import time, random
import numpy as np

from functools import reduce
from loader import *
from utils import *

from tqdm import tqdm, tqdm_notebook
from random import sample

def token_to_id(token):
    """ Lookup word ID for a token """
    return VECTORS.stoi(token)

def doc_to_tensor(document):
    """ Convert a sentence to a tensor """
    return to_var(torch.tensor([token_to_id(token) for token in document.tokens]))

# Load in corpus, lazily load in word vectors.
train_corpus = read_corpus('../data/train/')
VECTORS = LazyVectors()
VECTORS.set_vocab(train_corpus.vocab)


class Score(nn.Module):
    """ Generic scoring module """
    def __init__(self, embeds_dim, hidden_dim=150):
        super().__init__()

        self.score = nn.Sequential(
            nn.Linear(embeds_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.20),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.20),
            nn.Linear(hidden_dim, 1)
        )

    def forward(self, x):
        """ Output a scalar score for an input x """
        return self.score(x)


class Distance(nn.Module):
    """ Learned, continuous representations for: span size, width between spans """
    
    bins = [1,2,3,4,8,16,32,64]
    
    def __init__(self, distance_dim=20):
        super().__init__()

        self.dim = distance_dim
        self.embeds = nn.Sequential(
            nn.Embedding(len(self.bins)+1, distance_dim),
            nn.Dropout(0.20)
        )

    def forward(self, *args):
        """ Embedding table lookup """
        return self.embeds(self.stoi(*args)).squeeze()

    def stoi(self, num):
        """ Find which bin a number falls into """
        return to_cuda(torch.tensor(
            sum([True for i in self.bins if num >= i]), requires_grad=False
        ))


class Genre(nn.Module):
    """ Learned continuous representations for genre. Zeros if genre unknown. """
    
    genres = ['bc', 'bn', 'mz', 'nw', 'pt', 'tc', 'wb']
    _stoi = {genre: idx+1 for idx, genre in enumerate(genres)}
    
    def __init__(self, genre_dim=20):
        super().__init__()

        self.embeds = nn.Sequential(
            nn.Embedding(len(self.genres)+1, genre_dim, padding_idx=0),
            nn.Dropout(0.20)
        )
        
    def forward(self, genre):
        """ Embedding table lookup """
        return self.embeds(self.stoi(genre))
        
    def stoi(self, genre):
        """ Locate embedding id for genre """
        idx = self._stoi.get(genre)
        return torch.tensor(idx) if idx else torch.tensor(0)


class Speaker(nn.Module):
    """ Learned continuous representations for binary speaker. Zeros if speaker unknown. """

    def __init__(self, speaker_dim=20):
        super().__init__()    
        
        self.embeds = nn.Sequential(
            nn.Embedding(3, speaker_dim, padding_idx=0),
            nn.Dropout(0.20)
        )
        
    def forward(self, s1, s2):
        """ Embedding table lookup """
        # Same speaker
        if s1.speaker == s2.speaker:
            idx = torch.tensor(1)
            
        # Different speakers
        elif s1.speaker != s2.speaker:
            idx = torch.tensor(2)
            
        # No speaker
        else:
            idx = torch.tensor(0)
        
        return self.embeds(idx)


class CharCNN(nn.Module):
    """ Character-level CNN. Contains character embeddings. """

    unk_idx = 1
    vocab = train_corpus.char_vocab
    _stoi = {char: idx+2 for idx, char in enumerate(vocab)}
    pad_size = 15
    
    def __init__(self, filters, char_dim=8):
        super().__init__()
                
        self.embeddings = nn.Embedding(len(self.vocab)+2, char_dim, padding_idx=0)
        self.convs = nn.ModuleList([nn.Conv1d(in_channels=self.pad_size, 
                                              out_channels=filters, 
                                              kernel_size=n) for n in (3,4,5)])
        self.cnn_dropout = nn.Dropout(0.20)

    def forward(self, doc):
        """ Compute filter-dimensional character-level features for each doc token """
        embedded = self.embeddings(self.doc_to_batch(doc))
        convolved = torch.cat([F.relu(conv(embedded)) for conv in self.convs], dim=2)
        pooled = F.max_pool1d(convolved, convolved.shape[2])
        output = self.cnn_dropout(pooled).squeeze()
        return output
        
    def doc_to_batch(self, doc):
        """ Batch-ify a document class instance for CharCNN embeddings """
        tokens = [self.token_to_idx(token) for token in doc.tokens]
        batch = self.pad_and_stack(tokens)
        return batch
        
    def token_to_idx(self, token):
        """ Convert a token to its character lookup ids """
        return torch.tensor([self.stoi(c) for c in token])
    
    def pad_and_stack(self, tokens):
        """ Pad and stack an uneven tensor of token lookup ids """
        skimmed = [t[:self.pad_size] for t in tokens]
        
        lens = [len(t) for t in skimmed]
                    
        padded = [F.pad(t, (0, self.pad_size-length))
                  for t, length in zip(skimmed, lens)]
        
        return torch.stack(padded)
    
    def stoi(self, char):
        """ Lookup char id. <PAD> is 0, <UNK> is 1. """
        idx = self._stoi.get(char)
        return idx if idx else self.unk_idx 


class DocumentEncoder(nn.Module):
    def __init__(self, hidden_dim, char_filters):
        """ Document encoder for tokens """
        super().__init__()

        weights = VECTORS.weights()

        self.embeddings = nn.Embedding(weights.shape[0], weights.shape[1])
        self.embeddings.weight.data.copy_(weights)
        self.char_embeddings = CharCNN(char_filters)

        self.lstm = nn.LSTM(weights.shape[1], hidden_dim, bidirectional=True, batch_first=True)
        self.emb_dropout, self.lstm_dropout = nn.Dropout(0.50), nn.Dropout(0.20)

    def forward(self, document):
        """ Convert document words to ids, embed them, pass through LSTM. """
        # Convert document tokens to look up ids
        tensor = doc_to_tensor(document)
        tensor = tensor.unsqueeze(0)

        # Embed the tokens, regularize
        embeds = self.embeddings(tensor)
        embeds = self.emb_dropout(embeds)
        
        char_embeds = self.char_embeddings(document).unsqueeze(0)
        full_embeds = torch.cat((embeds, char_embeds), dim=2)

        # Pass an LSTM over the embeds, regularize
        states, _ = self.lstm(embeds)
        states = self.lstm_dropout(states)

        return states.squeeze(), full_embeds.squeeze()


class MentionScore(nn.Module):
    """ Mention scoring module """
    def __init__(self, gi_dim, attn_dim, distance_dim):
        super().__init__()

        self.attention = Score(attn_dim)
        self.width = Distance(distance_dim)
        self.score = Score(gi_dim)

    def forward(self, states, embeds, document, LAMBDA=0.40):
        """ Compute unary mention score for each span

        """
        # Compute first part of attention over span states
        attns = self.attention(states)

        spans = []

        for span in document.spans: # could probably deprecate this
            # Start index, end index of the span
            i1, i2 = span[0], span[-1]
            
            # Speaker
            speaker = s_to_speaker(span, document.speakers)

            # Embeddings, hidden states, raw attn scores for tokens
            span_embeds = embeds[i1:i2+1]
            span_attn = attns[i1:i2+1]

            # Compute the rest of the attention
            attn = F.softmax(span_attn, dim=0)
            attn = sum(attn * span_embeds)

            # Lookup embedding for width of spans
            size = self.width(len(span))

            # Final span representation g_i
            g_i = torch.cat([states[i1], states[i2], attn, size])
            spans.append(Span(i1, i2, g_i, speaker))
            
        # Compute each span's unary mention score
        mention_reprs = torch.stack([s.g for s in spans])
        mention_scores = self.score(mention_reprs).squeeze()

        # Update the span object
        spans = [
            attr.evolve(span, si=si)
            for span, si in zip(spans, mention_scores)
        ]

        return spans


class PairwiseScore(nn.Module):
    """ Coreference pair scoring module """
    def __init__(self, gij_dim, distance_dim, genre_dim, speaker_dim):
        super().__init__()

        self.distance = Distance(distance_dim)
        self.genre = Genre(genre_dim)
        self.speaker = Speaker(speaker_dim)
        
        self.score = Score(gij_dim)

    def forward(self, spans, genre, K=250):
        """ Compute pairwise score for spans and their up to K antecedents

        """
        # Consider only top K antecedents
        spans = [
            attr.evolve(span, yi=spans[max(0, idx-K):idx])
            for idx, span in enumerate(spans)
        ]

        # Get s_ij representations
        pairs = torch.stack([
            torch.cat([i.g, j.g, i.g*j.g, 
                       self.distance(i.i2-j.i1), 
                       self.genre(genre), 
                       self.speaker(i, j)])
            for i in spans for j in i.yi
        ])

        # Score pairs of spans for coreference link
        pairwise_scores = self.score(pairs).squeeze()

        # Indices for pairs indexing back into pairwise_scores
        sa_idx = pairwise_indexes(spans)

        spans_ij = []
        for span, (i1, i2) in zip(spans, pair(sa_idx)):

            # sij = score between span i, span j
            sij = [
                (span.si + yi.si + pair)
                for yi, pair in zip(span.yi, pairwise_scores[i1:i2])
            ]

            # Dummy variable for if the span is not a mention
            epsilon = to_var(torch.tensor(0.))
            sij = torch.stack(sij + [epsilon])

            # Update span object
            spans_ij.append(attr.evolve(span, sij=sij))

        # Update spans with set of possible antecedents' indices
        spans = [
            attr.evolve(span, yi_idx = [((y.i1, y.i2), (span.i1, span.i2)) for y in span.yi])
            for span in spans_ij
        ]

        return spans


class CorefScore(nn.Module):
    """ Super class to compute coreference links between spans """
    def __init__(self, embeds_dim, 
                       hidden_dim, 
                       char_filters=50, 
                       distance_dim=20, 
                       genre_dim=20,
                       speaker_dim=20):
        
        super().__init__()

        # Forward and backward pass over the document
        attn_dim = hidden_dim*2
        
        # Forward and backward passes, avg'd attn over embeddings, span width
        gi_dim = attn_dim*2 + embeds_dim + distance_dim
        
        # gi, gj, gi*gj, distance between gi and gj
        gij_dim = gi_dim*3 + distance_dim + genre_dim + speaker_dim

        # Initialize modules
        self.encode_doc = DocumentEncoder(hidden_dim, char_filters)
        self.score_spans = MentionScore(gi_dim, attn_dim, distance_dim)
        self.score_pairs = PairwiseScore(gij_dim, distance_dim, genre_dim, speaker_dim)

    def forward(self, document):
        """ Enocde document
            Predict unary mention scores, prune them
            Predict pairwise coreference scores
        """
        # Encode the document, keep the LSTM hidden states and embedded tokens
        states, embeds = self.encode_doc(document)

        # Get mention scores for each span
        spans = self.score_spans(states, embeds, document)

        # Prune the spans by decreasing mention score
        spans = prune(spans, len(document))

        # Get pairwise scores for each span combo
        spans = self.score_pairs(spans, document.genre)

        return spans


class Trainer:
    """ Class dedicated to training the model """
    def __init__(self, train_corpus, model, lr=1e-3):
        self.train_corpus = list(train_corpus)
        self.model = to_cuda(model)
        
        self.optimizer = optim.Adam(params=[p for p in self.model.parameters() if p.requires_grad], lr=lr)
        self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, step_size=100, gamma=0.001)

    def train(self, num_epochs, *args, **kwargs):
        """ Train a model """
        for epoch in range(1, num_epochs+1):
            self.train_epoch(epoch, *args, **kwargs)

    def train_epoch(self, epoch, steps=25):
        """ Run a training epoch over 'steps' documents """
        # Set model to train (enables dropout)
        self.model.train()

        # Randomly sample documents from the train corpus
        docs = random.sample(self.train_corpus, steps)

        epoch_loss, epoch_recall = [], []
        for doc in tqdm_notebook(docs):

            # Randomly truncate document to up to 50 sentences
            document = doc.truncate()

            # Compute loss, number gold links found, total gold links
            loss, recall, total_golds = self.train_doc(document)

            # Track stats by document for debugging
            print(document, '| Loss: %f | Coreferences found: %d | Total Coreferences: %d' % (loss, recall, total_golds))
            epoch_loss.append(loss), epoch_recall.append(recall)

            # Step the learning rate decrease scheduler
            self.scheduler.step()

        print('Epoch: %d | Loss: %f | Recall: %f' % (epoch, np.mean(epoch_loss), np.mean(epoch_recall)))

    def train_doc(self, document, CLIP=5):
        """ Compute loss for a forward pass over a document """
        # Extract gold coreference links
        gold_corefs, total_golds = extract_gold_corefs(document)

        # Zero out optimizer gradients
        self.optimizer.zero_grad()

        losses, golds_found = [], []
        for span in self.model(document):

            # Check which of these tuples are in the gold set, if any
            gold_idx = [
                idx for idx, link in enumerate(span.yi_idx)
                if link in gold_corefs
            ]

            # If gold_pred_idx is empty, set gold to dummy
            if not gold_idx:
                gold_idx = [len(span.sij)-1]
            else:
                golds_found.append(len(gold_idx))

            # Conditional probability distribution over all possible previous spans
            probs = F.softmax(span.sij, dim=0)

            # Marginal log-likelihood of correct antecedents implied by gold clustering
            mass = torch.log(sum([probs[i] for i in gold_idx]))

            # Save the loss for this span
            losses.append(mass)

        # Negative marginal log-likelihood for minimizing, backpropagate
        loss = sum(losses) * -1
        loss.backward()

        # Clip parameters
        nn.utils.clip_grad_norm_(self.model.parameters(), CLIP)

        # Step the optimizer
        self.optimizer.step()

        # Compute recall
        recall = sum(golds_found)

        return loss.item(), recall, total_golds

    def save_model(self, savepath):
        """ Save model state dictionary """
        torch.save(self.model.state_dict(), savepath + '.pth')

    def load_model(self, loadpath):
        """ Load state dictionary into model """
        state = torch.load(loadpath)
        self.model.load_state_dict(state)
        self.model = to_cuda(self.model)


model = CorefScore(embeds_dim=350, hidden_dim=200)
trainer = Trainer(train_corpus, model)
trainer.train(100)

